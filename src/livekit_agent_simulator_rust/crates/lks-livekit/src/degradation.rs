//! Audio degradation effects (port of `audio/degradation.py`) — packet-loss,
//! breaking-up, echo, phone-quality, static. Deterministic (seeded), applied
//! per PCM frame so the agent hears imperfect audio like a real caller.

use rand::{rngs::StdRng, RngExt, SeedableRng};

pub const SUPPORTED_EFFECTS: [&str; 5] = [
    "breaking_up",
    "echo",
    "packet_loss",
    "phone_quality",
    "static",
];

type Samples = Vec<i16>;

/// One PCM-frame effect in the chain (packet-loss / echo / …).
pub type PcmEffect = Box<dyn Fn(&[u8]) -> Vec<u8> + Send>;
pub type PcmEffectChain = Vec<PcmEffect>;

fn pcm_to_samples(pcm: &[u8]) -> Samples {
    let mut s: Vec<i16> = Vec::with_capacity(pcm.len() / 2);
    let mut i = 0;
    while i + 1 < pcm.len() {
        s.push(i16::from_le_bytes([pcm[i], pcm[i + 1]]));
        i += 2;
    }
    s
}

fn samples_to_pcm(s: &Samples) -> Vec<u8> {
    let mut out = Vec::with_capacity(s.len() * 2);
    for v in s {
        out.extend_from_slice(&v.to_le_bytes());
    }
    out
}

fn dropout(samples: &mut Samples, chunk_samples: usize, probability: f64, rng: &mut StdRng) {
    if probability <= 0.0 || samples.is_empty() {
        return;
    }
    let chunk = chunk_samples.clamp(1, samples.len());
    let mut i = 0;
    while i < samples.len() {
        if rng.random::<f64>() < probability {
            let n = chunk.min(samples.len() - i);
            for s in samples.iter_mut().skip(i).take(n) {
                *s = 0;
            }
        }
        i += chunk;
    }
}

fn seed_rng(parts: &[&str]) -> StdRng {
    let seed = parts.join("|");
    // Hash the string into a [u8; 32] seed (deterministic across runs).
    let mut h = 0xcbf29ce484222325u64;
    for b in seed.bytes() {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    let mut bytes = [0u8; 32];
    for (i, b) in bytes.iter_mut().enumerate() {
        h = h.wrapping_mul(0x100000001b3) ^ (i as u64).wrapping_add(1);
        *b = (h >> ((i % 8) * 8)) as u8;
    }
    StdRng::from_seed(bytes)
}

/// Build the ordered effect chain from `speech_conditions.effects`.
pub fn resolve_audio_effects(spec: Option<&serde_json::Value>) -> Result<PcmEffectChain, String> {
    let mut chain: PcmEffectChain = Vec::new();
    let Some(spec) = spec else { return Ok(chain) };
    let entries: Vec<(String, serde_json::Value)> = match spec {
        serde_json::Value::Object(m) => m.iter().map(|(k, v)| (k.clone(), v.clone())).collect(),
        serde_json::Value::Array(a) => a
            .iter()
            .filter_map(|v| {
                v.as_str()
                    .map(|s| (s.to_string(), serde_json::Value::Bool(true)))
            })
            .collect(),
        _ => return Ok(chain),
    };
    for (name, kw) in entries {
        let kwargs = kw.as_object().cloned().unwrap_or_default();
        match name.as_str() {
            "packet_loss" => {
                let probability = kwargs
                    .get("probability")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(0.05);
                let chunk_ms = kwargs
                    .get("chunk_ms")
                    .and_then(|v| v.as_i64())
                    .unwrap_or(20) as usize;
                let chunk = (24_000usize * chunk_ms).max(1) / 1000;
                let rng = std::cell::RefCell::new(seed_rng(&[
                    "packet_loss",
                    &probability.to_string(),
                    &chunk.to_string(),
                ]));
                chain.push(Box::new(move |pcm: &[u8]| {
                    if probability <= 0.0 || pcm.is_empty() {
                        return pcm.to_vec();
                    }
                    let mut s = pcm_to_samples(pcm);
                    dropout(&mut s, chunk, probability, &mut rng.borrow_mut());
                    samples_to_pcm(&s)
                }));
            }
            "breaking_up" => {
                let probability = kwargs
                    .get("probability")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(0.2);
                let chunk = 24_000 * 100 / 1000;
                let rng = std::cell::RefCell::new(seed_rng(&[
                    "breaking_up",
                    &probability.to_string(),
                    &chunk.to_string(),
                ]));
                chain.push(Box::new(move |pcm: &[u8]| {
                    if pcm.is_empty() {
                        return pcm.to_vec();
                    }
                    let mut s = pcm_to_samples(pcm);
                    dropout(&mut s, chunk, probability, &mut rng.borrow_mut());
                    samples_to_pcm(&s)
                }));
            }
            "echo" => {
                let delay_ms_raw = kwargs
                    .get("delay_ms")
                    .and_then(|v| v.as_i64())
                    .unwrap_or(200);
                if delay_ms_raw <= 0 {
                    return Err(format!(
                        "echo delay_ms must be positive (got {delay_ms_raw})"
                    ));
                }
                let delay_ms = delay_ms_raw as usize;
                let decay = kwargs.get("decay").and_then(|v| v.as_f64()).unwrap_or(0.5);
                let delay_samples = 24_000usize * delay_ms / 1000;
                chain.push(Box::new(move |pcm: &[u8]| {
                    if pcm.is_empty() {
                        return pcm.to_vec();
                    }
                    let samples = pcm_to_samples(pcm);
                    let mut out = samples.clone();
                    for i in 0..samples.len() {
                        if i >= delay_samples {
                            let v = out[i] as f64 + (samples[i - delay_samples] as f64 * decay);
                            out[i] = v.round().clamp(-32768.0, 32767.0) as i16;
                        }
                    }
                    samples_to_pcm(&out)
                }));
            }
            "phone_quality" => {
                let window = 24_000usize / 6000;
                chain.push(Box::new(move |pcm: &[u8]| {
                    if pcm.is_empty() {
                        return pcm.to_vec();
                    }
                    let samples = pcm_to_samples(pcm);
                    let mut out = vec![0i16; samples.len()];
                    let mut acc: i64 = 0;
                    for (i, &s) in samples.iter().enumerate() {
                        acc += s as i64;
                        if i >= window {
                            acc -= samples[i - window] as i64;
                        }
                        let mut avg = (acc / window as i64) as i32;
                        if avg > 8000 {
                            avg = 8000 + (avg - 8000) / 2;
                        } else if avg < -8000 {
                            avg = -8000 - (avg + 8000) / 2;
                        }
                        out[i] = avg.clamp(-32768, 32767) as i16;
                    }
                    samples_to_pcm(&out)
                }));
            }
            "static" => {
                let intensity = kwargs
                    .get("intensity")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(0.05);
                if !(0.0..=1.0).contains(&intensity) {
                    return Err(format!(
                        "static intensity must be in [0.0, 1.0] (got {intensity})"
                    ));
                }
                let rng = std::cell::RefCell::new(seed_rng(&["static", &intensity.to_string()]));
                chain.push(Box::new(move |pcm: &[u8]| {
                    if pcm.is_empty() {
                        return pcm.to_vec();
                    }
                    let mut out = pcm_to_samples(pcm);
                    let amp = (32767.0 * intensity).round();
                    for v in out.iter_mut() {
                        // Gaussian-ish via two uniforms (Box-Muller-lite).
                        let u: f64 = rng.borrow_mut().random::<f64>();
                        let noise = (u - 0.5) * 2.0 * amp;
                        let nv = *v as f64 + noise;
                        *v = nv.round().clamp(-32768.0, 32767.0) as i16;
                    }
                    samples_to_pcm(&out)
                }));
            }
            other => {
                return Err(format!(
                    "unknown audio effect: {other} (supported: {})",
                    SUPPORTED_EFFECTS.join(", ")
                ));
            }
        }
    }
    Ok(chain)
}

/// Apply the effect chain to one PCM frame.
pub fn apply_effects(chain: &PcmEffectChain, pcm: &[u8]) -> Vec<u8> {
    let mut out = pcm.to_vec();
    for effect in chain {
        out = effect(&out);
    }
    out
}
