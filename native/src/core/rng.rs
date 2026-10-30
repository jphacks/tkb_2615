//! `CounterRNG` (`core/rng.py`). Every draw's key is written by `Key`, so the labels can be
//! changed in one place at step 4.

use sha2::{Digest, Sha256};

use crate::write_json_string;

/// `CounterRNG.random`: SHA-256 of the seed and the JSON key, the first eight bytes as a
/// fraction of 2**64.
pub(crate) struct KeyedDraws {
    seeded: Sha256,
}

impl KeyedDraws {
    /// `seed` is `str(seed)` of a non-negative int.
    pub(crate) fn new(seed: &str) -> Self {
        let mut seeded = Sha256::new();
        seeded.update(b"sheltersim:sha256-keyed-v1\0");
        seeded.update(seed.as_bytes());
        seeded.update(b"\0");
        Self { seeded }
    }

    fn draw(&self, key: &[u8]) -> f64 {
        let digest = self.seeded.clone().chain_update(key).finalize();
        let mut first = [0u8; 8];
        first.copy_from_slice(&digest[..8]);
        // u64 to f64 and int / int both round to nearest, ties to even, and the division by a
        // power of two is exact.
        u64::from_be_bytes(first) as f64 / 18_446_744_073_709_551_616.0
    }
}

/// The key parts of a draw, written as `json.dumps(list(parts), ensure_ascii=False,
/// separators=(",", ":"))`. Rules that draw in a loop keep a `mark` of the common parts and
/// `rewind` to it, which avoids writing them again.
#[derive(Clone)]
pub(crate) struct Key {
    bytes: Vec<u8>,
}

impl Key {
    pub(crate) fn new() -> Self {
        let mut bytes = Vec::with_capacity(64);
        bytes.push(b'[');
        Self { bytes }
    }

    /// A key that starts with the rule's label.
    pub(crate) fn labelled(label: &str) -> Self {
        let mut key = Self::new();
        key.text(label);
        key
    }

    fn separate(&mut self) {
        if self.bytes.len() > 1 {
            self.bytes.push(b',');
        }
    }

    pub(crate) fn text(&mut self, part: &str) -> &mut Self {
        self.separate();
        write_json_string(&mut self.bytes, part);
        self
    }

    /// A part already written as a JSON string, kept per person by the callers.
    pub(crate) fn quoted(&mut self, part: &[u8]) -> &mut Self {
        self.separate();
        self.bytes.extend_from_slice(part);
        self
    }

    pub(crate) fn int(&mut self, part: i64) -> &mut Self {
        self.separate();
        self.bytes
            .extend_from_slice(itoa::Buffer::new().format(part).as_bytes());
        self
    }

    pub(crate) fn mark(&self) -> usize {
        self.bytes.len()
    }

    pub(crate) fn rewind(&mut self, mark: usize) {
        self.bytes.truncate(mark);
    }

    /// The draw for these parts. The key is unchanged, so more parts can follow.
    pub(crate) fn draw(&mut self, draws: &KeyedDraws) -> f64 {
        self.bytes.push(b']');
        let value = draws.draw(&self.bytes);
        self.bytes.pop();
        value
    }
}
