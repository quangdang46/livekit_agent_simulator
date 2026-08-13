//! Audio encoding utilities for the Gemini Live API.
//!
//! # Wire format
//!
//! | Direction | Encoding          | Sample rate | MIME                      |
//! |-----------|-------------------|-------------|---------------------------|
//! | Input     | 16-bit signed LE PCM | 16 kHz   | `audio/pcm;rate=16000`    |
//! | Output    | 16-bit signed LE PCM | 24 kHz   | `audio/pcm;rate=24000`    |
//!
//! Audio data is base64-encoded inside JSON text frames.
//!
//! # Chunk sizing
//!
//! Aim for **100–250 ms** per chunk:
//! - 16 kHz × 2 bytes × 0.1 s = 3,200 bytes raw → ~4,300 bytes base64
//! - 16 kHz × 2 bytes × 0.25 s = 8,000 bytes raw → ~10,700 bytes base64
//!
//! Chunks smaller than ~20 ms waste bandwidth on WebSocket frame overhead;
//! chunks larger than ~250 ms add perceptible latency.

use base64::Engine;

/// Input audio MIME type (16-bit LE PCM, 16 kHz).
pub const INPUT_AUDIO_MIME: &str = "audio/pcm;rate=16000";

/// Output audio MIME type (16-bit LE PCM, 24 kHz).
pub const OUTPUT_AUDIO_MIME: &str = "audio/pcm;rate=24000";

/// Input audio sample rate in Hz.
pub const INPUT_SAMPLE_RATE: u32 = 16_000;

/// Output audio sample rate in Hz.
pub const OUTPUT_SAMPLE_RATE: u32 = 24_000;

/// Zero-allocation audio encoder for the streaming hot path.
///
/// Both [`encode_f32`](Self::encode_f32) and [`encode_i16_le`](Self::encode_i16_le)
/// write into pre-allocated internal buffers and return a borrowed `&str`.
/// After the first call that establishes capacity, subsequent calls produce
/// **no heap allocations** — critical for real-time audio where this runs
/// every 100–250 ms.
///
/// # Performance
///
/// For maximum throughput, keep a single `AudioEncoder` instance alive across
/// the streaming loop and pair it with [`Session::send_raw`](crate::session::Session::send_raw)
/// to avoid the extra allocation in [`Session::send_audio`](crate::session::Session::send_audio).
///
/// ```rust,no_run
/// # use gemini_live::audio::{AudioEncoder, INPUT_AUDIO_MIME};
/// # use gemini_live::types::*;
/// # fn example(session: &gemini_live::session::Session, pcm_bytes: &[u8]) {
/// let mut enc = AudioEncoder::new();
/// // In a streaming loop:
/// let b64 = enc.encode_i16_le(pcm_bytes);
/// // Build the message — only the to_owned() here allocates:
/// let msg = ClientMessage::RealtimeInput(RealtimeInput {
///     audio: Some(Blob { data: b64.to_owned(), mime_type: INPUT_AUDIO_MIME.into() }),
///     video: None, text: None, activity_start: None, activity_end: None,
///     audio_stream_end: None,
/// });
/// # }
/// ```
///
/// # Example
///
/// ```
/// use gemini_live::audio::AudioEncoder;
///
/// let mut enc = AudioEncoder::new();
/// let samples: Vec<f32> = vec![0.0, 0.5, -0.5, 1.0, -1.0];
/// let base64 = enc.encode_f32(&samples);
/// assert!(!base64.is_empty());
/// ```
pub struct AudioEncoder {
    pcm_buf: Vec<u8>,
    b64_buf: String,
}

impl AudioEncoder {
    /// Create a new encoder pre-allocated for ~250 ms at 16 kHz.
    pub fn new() -> Self {
        Self {
            // 250 ms × 16 kHz × 2 bytes = 8,000 bytes
            pcm_buf: Vec::with_capacity(8_000),
            // base64 expands by ~4/3
            b64_buf: String::with_capacity(11_000),
        }
    }

    /// Encode `f32` samples (range `[-1.0, 1.0]`) to base64 i16-LE PCM.
    ///
    /// Values outside `[-1.0, 1.0]` are clamped.  The returned `&str` borrows
    /// the encoder's internal buffer and is valid until the next `encode_*`
    /// call.
    pub fn encode_f32(&mut self, samples: &[f32]) -> &str {
        self.pcm_buf.clear();
        for &s in samples {
            let clamped = s.clamp(-1.0, 1.0);
            let i16_val = (clamped * 32767.0) as i16;
            self.pcm_buf.extend_from_slice(&i16_val.to_le_bytes());
        }
        self.b64_buf.clear();
        base64::engine::general_purpose::STANDARD.encode_string(&self.pcm_buf, &mut self.b64_buf);
        &self.b64_buf
    }

    /// Encode raw i16 little-endian PCM bytes to base64 (zero-conversion path).
    ///
    /// Use this when the audio source already provides i16-LE samples.
    pub fn encode_i16_le(&mut self, pcm: &[u8]) -> &str {
        self.b64_buf.clear();
        base64::engine::general_purpose::STANDARD.encode_string(pcm, &mut self.b64_buf);
        &self.b64_buf
    }
}

impl Default for AudioEncoder {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encode_f32_silence() {
        let mut enc = AudioEncoder::new();
        let samples = vec![0.0f32; 100];
        let b64 = enc.encode_f32(&samples);
        let decoded = base64::engine::general_purpose::STANDARD
            .decode(b64)
            .unwrap();
        // 100 samples × 2 bytes each
        assert_eq!(decoded.len(), 200);
        // All zeros → silence
        assert!(decoded.iter().all(|&b| b == 0));
    }

    #[test]
    fn encode_f32_boundary_values() {
        let mut enc = AudioEncoder::new();
        let samples = vec![1.0f32, -1.0, 0.5, -0.5];
        let b64 = enc.encode_f32(&samples);
        let decoded = base64::engine::general_purpose::STANDARD
            .decode(b64)
            .unwrap();
        assert_eq!(decoded.len(), 8);

        // Parse back as i16-LE
        let s0 = i16::from_le_bytes([decoded[0], decoded[1]]);
        let s1 = i16::from_le_bytes([decoded[2], decoded[3]]);
        let s2 = i16::from_le_bytes([decoded[4], decoded[5]]);
        let s3 = i16::from_le_bytes([decoded[6], decoded[7]]);

        assert_eq!(s0, 32767); //  1.0 → i16::MAX
        assert_eq!(s1, -32767); // -1.0 → clamped
        assert_eq!(s2, 16383); //  0.5
        assert_eq!(s3, -16383); // -0.5
    }

    #[test]
    fn encode_f32_clamps_overflow() {
        let mut enc = AudioEncoder::new();
        let samples = vec![2.0f32, -2.0];
        let b64 = enc.encode_f32(&samples);
        let decoded = base64::engine::general_purpose::STANDARD
            .decode(b64)
            .unwrap();
        let s0 = i16::from_le_bytes([decoded[0], decoded[1]]);
        let s1 = i16::from_le_bytes([decoded[2], decoded[3]]);
        assert_eq!(s0, 32767);
        assert_eq!(s1, -32767);
    }

    #[test]
    fn encode_i16_le_roundtrip() {
        let mut enc = AudioEncoder::new();
        let raw_pcm: Vec<u8> = vec![0x01, 0x02, 0x03, 0x04];
        let b64 = enc.encode_i16_le(&raw_pcm);
        let decoded = base64::engine::general_purpose::STANDARD
            .decode(b64)
            .unwrap();
        assert_eq!(decoded, raw_pcm);
    }

    #[test]
    fn encoder_reuses_buffer() {
        let mut enc = AudioEncoder::new();
        let _ = enc.encode_f32(&[0.0; 1000]);
        let cap_after_first = enc.b64_buf.capacity();
        let _ = enc.encode_f32(&[0.0; 100]);
        // Capacity should not shrink (buffer reuse)
        assert!(enc.b64_buf.capacity() >= cap_after_first);
    }
}
