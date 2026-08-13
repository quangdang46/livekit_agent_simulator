//! Benchmarks for the streaming hot path.
//!
//! Send path: raw PCM → base64 encode → assemble JSON → (WebSocket send)
//! Recv path: (WebSocket recv) → JSON parse → base64 decode → event decomposition
//!
//! Primary chunk size: **40 ms** (typical real-time audio frame).
//! 16 kHz × 2 bytes × 40 ms = 1,280 bytes raw PCM.
//!
//! Run with: cargo bench -p gemini-live

use std::hint::black_box;

use base64::Engine;
use criterion::{BenchmarkId, Criterion, criterion_group, criterion_main};

use gemini_live::audio::AudioEncoder;
use gemini_live::codec;
use gemini_live::types::*;

// ── Chunk sizes (16 kHz mono i16 PCM) ────────────────────────────────────────

const CHUNK_20MS: usize = 16_000 * 2 * 20 / 1000; //    640 bytes
const CHUNK_40MS: usize = 16_000 * 2 * 40 / 1000; //  1,280 bytes
const CHUNK_100MS: usize = 16_000 * 2 * 100 / 1000; //  3,200 bytes

/// Output audio chunk: 40 ms of 24 kHz mono i16 PCM = 1,920 bytes
const OUTPUT_CHUNK_40MS: usize = 24_000 * 2 * 40 / 1000;

fn pcm_bytes(size: usize) -> Vec<u8> {
    (0..size).map(|i| (i % 256) as u8).collect()
}

fn f32_samples(count: usize) -> Vec<f32> {
    (0..count)
        .map(|i| i as f32 / count as f32 * 2.0 - 1.0)
        .collect()
}

// ── Audio Encoder ────────────────────────────────────────────────────────────

fn bench_audio_encoder(c: &mut Criterion) {
    let mut group = c.benchmark_group("audio_encoder");

    for (label, size) in [
        ("20ms", CHUNK_20MS),
        ("40ms", CHUNK_40MS),
        ("100ms", CHUNK_100MS),
    ] {
        let pcm = pcm_bytes(size);
        group.bench_with_input(BenchmarkId::new("encode_i16_le", label), &pcm, |b, pcm| {
            let mut enc = AudioEncoder::new();
            b.iter(|| {
                let result = enc.encode_i16_le(pcm);
                black_box(result.len());
            });
        });
    }

    for (label, count) in [("20ms/320", 320), ("40ms/640", 640), ("100ms/1600", 1_600)] {
        let samples = f32_samples(count);
        group.bench_with_input(
            BenchmarkId::new("encode_f32", label),
            &samples,
            |b, samples| {
                let mut enc = AudioEncoder::new();
                b.iter(|| {
                    let result = enc.encode_f32(samples);
                    black_box(result.len());
                });
            },
        );
    }

    group.finish();
}

// ── Codec Encode (send path) ─────────────────────────────────────────────────

fn bench_codec_encode(c: &mut Criterion) {
    let mut group = c.benchmark_group("codec_encode");

    // Audio message (40ms chunk — the hot-path case)
    let pcm = pcm_bytes(CHUNK_40MS);
    let b64 = base64::engine::general_purpose::STANDARD.encode(&pcm);
    let audio_msg = ClientMessage::RealtimeInput(RealtimeInput {
        audio: Some(Blob {
            data: b64,
            mime_type: "audio/pcm;rate=16000".into(),
        }),
        video: None,
        text: None,
        activity_start: None,
        activity_end: None,
        audio_stream_end: None,
    });

    group.bench_function("audio_40ms", |b| {
        b.iter(|| black_box(codec::encode(&audio_msg).unwrap()));
    });

    // Text message (small)
    let text_msg = ClientMessage::RealtimeInput(RealtimeInput {
        text: Some("Hello, how are you?".into()),
        audio: None,
        video: None,
        activity_start: None,
        activity_end: None,
        audio_stream_end: None,
    });

    group.bench_function("text", |b| {
        b.iter(|| black_box(codec::encode(&text_msg).unwrap()));
    });

    group.finish();
}

// ── Codec Decode (recv path) ──────────────────────────────────────────────────

fn bench_codec_decode(c: &mut Criterion) {
    let mut group = c.benchmark_group("codec_decode");

    // Text response
    let text_json = r#"{"serverContent":{"modelTurn":{"parts":[{"text":"Hello! How can I help you today?"}]},"turnComplete":true}}"#;
    group.bench_function("text_turn_complete", |b| {
        b.iter(|| black_box(codec::decode(text_json).unwrap()));
    });

    // Audio response (40ms of 24kHz output)
    let audio_pcm = pcm_bytes(OUTPUT_CHUNK_40MS);
    let audio_b64 = base64::engine::general_purpose::STANDARD.encode(&audio_pcm);
    let audio_json = format!(
        r#"{{"serverContent":{{"modelTurn":{{"parts":[{{"inlineData":{{"data":"{audio_b64}","mimeType":"audio/pcm;rate=24000"}}}}]}}}}}}"#,
    );
    group.bench_function("audio_40ms", |b| {
        b.iter(|| black_box(codec::decode(&audio_json).unwrap()));
    });

    // Combined (text + usage metadata)
    let combined_json = r#"{"serverContent":{"modelTurn":{"parts":[{"text":"Hi"}]},"turnComplete":true},"usageMetadata":{"promptTokenCount":100,"responseTokenCount":50,"totalTokenCount":150}}"#;
    group.bench_function("text_with_usage", |b| {
        b.iter(|| black_box(codec::decode(combined_json).unwrap()));
    });

    group.finish();
}

// ── into_events (decomposition) ──────────────────────────────────────────────

fn bench_into_events(c: &mut Criterion) {
    let mut group = c.benchmark_group("into_events");

    // Audio — exercises base64 decode
    let audio_pcm = pcm_bytes(OUTPUT_CHUNK_40MS);
    let audio_b64 = base64::engine::general_purpose::STANDARD.encode(&audio_pcm);
    let audio_json = format!(
        r#"{{"serverContent":{{"modelTurn":{{"parts":[{{"inlineData":{{"data":"{audio_b64}","mimeType":"audio/pcm;rate=24000"}}}}]}}}}}}"#,
    );
    group.bench_function("audio_40ms", |b| {
        b.iter_batched(
            || codec::decode(&audio_json).unwrap(),
            |msg| black_box(codec::into_events(msg)),
            criterion::BatchSize::SmallInput,
        );
    });

    // Text (no base64, just decomposition)
    let text_json = r#"{"serverContent":{"modelTurn":{"parts":[{"text":"Hello"}]},"turnComplete":true},"usageMetadata":{"totalTokenCount":42}}"#;
    group.bench_function("text_with_usage", |b| {
        b.iter_batched(
            || codec::decode(text_json).unwrap(),
            |msg| black_box(codec::into_events(msg)),
            criterion::BatchSize::SmallInput,
        );
    });

    group.finish();
}

// ── Full Send Path (40ms) ────────────────────────────────────────────────────

fn bench_full_send_path(c: &mut Criterion) {
    let mut group = c.benchmark_group("full_send_path");
    let pcm = pcm_bytes(CHUNK_40MS);

    // With AudioEncoder (buffer reuse)
    group.bench_function("audio_40ms_encoder", |b| {
        let mut enc = AudioEncoder::new();
        b.iter(|| {
            let b64 = enc.encode_i16_le(&pcm);
            let msg = ClientMessage::RealtimeInput(RealtimeInput {
                audio: Some(Blob {
                    data: b64.to_owned(),
                    mime_type: "audio/pcm;rate=16000".into(),
                }),
                video: None,
                text: None,
                activity_start: None,
                activity_end: None,
                audio_stream_end: None,
            });
            black_box(codec::encode(&msg).unwrap());
        });
    });

    // Without AudioEncoder (current Session::send_audio path)
    group.bench_function("audio_40ms_naive", |b| {
        b.iter(|| {
            let b64 = base64::engine::general_purpose::STANDARD.encode(&pcm);
            let msg = ClientMessage::RealtimeInput(RealtimeInput {
                audio: Some(Blob {
                    data: b64,
                    mime_type: "audio/pcm;rate=16000".into(),
                }),
                video: None,
                text: None,
                activity_start: None,
                activity_end: None,
                audio_stream_end: None,
            });
            black_box(codec::encode(&msg).unwrap());
        });
    });

    group.finish();
}

// ── Full Recv Path (40ms) ────────────────────────────────────────────────────

fn bench_full_recv_path(c: &mut Criterion) {
    let mut group = c.benchmark_group("full_recv_path");

    let audio_pcm = pcm_bytes(OUTPUT_CHUNK_40MS);
    let audio_b64 = base64::engine::general_purpose::STANDARD.encode(&audio_pcm);
    let audio_json = format!(
        r#"{{"serverContent":{{"modelTurn":{{"parts":[{{"inlineData":{{"data":"{audio_b64}","mimeType":"audio/pcm;rate=24000"}}}}]}},"turnComplete":true}},"usageMetadata":{{"totalTokenCount":100}}}}"#,
    );

    group.bench_function("audio_40ms_decode_and_decompose", |b| {
        b.iter(|| {
            let msg = codec::decode(&audio_json).unwrap();
            black_box(codec::into_events(msg));
        });
    });

    group.finish();
}

criterion_group!(
    benches,
    bench_audio_encoder,
    bench_codec_encode,
    bench_codec_decode,
    bench_into_events,
    bench_full_send_path,
    bench_full_recv_path,
);
criterion_main!(benches);
