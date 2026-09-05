//! Local conversation recorder (port of `audio/local_recorder.py` core):
//! wall-clock stereo buffer (L=sim, R=agent) → 16 kHz PCM16 conversation.wav.

use std::sync::Mutex;

pub const DEFAULT_SAMPLE_RATE: u32 = 16_000;

#[derive(Debug, Clone)]
pub struct RecordResult {
    pub path: String,
    pub sample_rate: u32,
    pub duration_ms: i64,
    pub sim_samples: usize,
    pub agent_samples: usize,
}

/// Simple linear resample PCM16 mono.
fn resample_pcm16_mono(pcm: &[u8], src_rate: u32, dst_rate: u32) -> Vec<u8> {
    if src_rate == dst_rate || pcm.is_empty() {
        return pcm.to_vec();
    }
    let src: Vec<i16> = pcm
        .chunks_exact(2)
        .map(|c| i16::from_le_bytes([c[0], c[1]]))
        .collect();
    if src.is_empty() {
        return Vec::new();
    }
    // Nearest-neighbor resample (good enough for a forensic mix).
    let out_len = (src.len() as u64 * dst_rate as u64 / src_rate as u64) as usize;
    let mut out = Vec::with_capacity(out_len * 2);
    for i in 0..out_len {
        let src_idx = (i as u64 * src_rate as u64 / dst_rate as u64) as usize;
        let s = src[src_idx.min(src.len() - 1)];
        out.extend_from_slice(&s.to_le_bytes());
    }
    out
}

pub struct LocalConversationRecorder {
    pub sample_rate: u32,
    started_mono: Option<std::time::Instant>,
    sim: Vec<i16>,
    agent: Vec<i16>,
    finalized: bool,
    lock: Mutex<()>,
}

impl Default for LocalConversationRecorder {
    fn default() -> Self {
        Self {
            sample_rate: DEFAULT_SAMPLE_RATE,
            started_mono: None,
            sim: Vec::new(),
            agent: Vec::new(),
            finalized: false,
            lock: Mutex::new(()),
        }
    }
}

impl LocalConversationRecorder {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn started(&self) -> bool {
        self.started_mono.is_some()
    }

    /// Pin t=0 (call when the sim mic publishes).
    pub fn mark_start(&mut self) {
        if self.started_mono.is_none() {
            self.started_mono = Some(std::time::Instant::now());
        }
    }

    fn push_channel(
        channel: &mut Vec<i16>,
        pcm: &[u8],
        sample_rate: u32,
        dst_rate: u32,
        started: Option<std::time::Instant>,
    ) {
        if pcm.is_empty() {
            return;
        }
        let converted = resample_pcm16_mono(pcm, sample_rate, dst_rate);
        if converted.is_empty() {
            return;
        }
        let samples: Vec<i16> = converted
            .chunks_exact(2)
            .map(|c| i16::from_le_bytes([c[0], c[1]]))
            .collect();
        if let Some(t0) = started {
            // Wall-clock gap padding (sparse speech gets silence between turns).
            let expected_s = t0.elapsed().as_secs_f64() - (channel.len() as f64 / dst_rate as f64);
            if expected_s > 0.02 {
                let pad = (expected_s * dst_rate as f64) as usize;
                channel.extend(std::iter::repeat_n(0i16, pad));
            }
        }
        channel.extend(samples);
    }

    pub fn push_sim(&mut self, pcm: &[u8], sample_rate: u32) {
        let _g = self.lock.lock().unwrap();
        if self.finalized {
            return;
        }
        Self::push_channel(
            &mut self.sim,
            pcm,
            sample_rate,
            self.sample_rate,
            self.started_mono,
        );
    }

    pub fn push_agent(&mut self, pcm: &[u8], sample_rate: u32) {
        let _g = self.lock.lock().unwrap();
        if self.finalized {
            return;
        }
        Self::push_channel(
            &mut self.agent,
            pcm,
            sample_rate,
            self.sample_rate,
            self.started_mono,
        );
    }

    /// Write conversation.wav (16 kHz PCM16 stereo, L=sim R=agent).
    /// Time the recorder was created (when the first audio arrived).
    pub fn started_mono(&self) -> Option<std::time::Instant> {
        self.started_mono
    }

    pub fn save(&mut self, path: &std::path::Path) -> Result<RecordResult, String> {
        let _g = self.lock.lock().unwrap();
        self.finalized = true;
        let n = self.sim.len().max(self.agent.len());
        let spec = hound::WavSpec {
            channels: 2,
            sample_rate: self.sample_rate,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let mut writer = hound::WavWriter::create(path, spec).map_err(|e| e.to_string())?;
        for i in 0..n {
            let s = self.sim.get(i).copied().unwrap_or(0);
            let a = self.agent.get(i).copied().unwrap_or(0);
            writer.write_sample(s).map_err(|e| e.to_string())?;
            writer.write_sample(a).map_err(|e| e.to_string())?;
        }
        writer.finalize().map_err(|e| e.to_string())?;
        Ok(RecordResult {
            path: path.to_string_lossy().into_owned(),
            sample_rate: self.sample_rate,
            duration_ms: (n as i64 * 1000 / self.sample_rate as i64),
            sim_samples: self.sim.len(),
            agent_samples: self.agent.len(),
        })
    }
}
