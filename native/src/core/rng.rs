//! `CounterRNG` (`core/rng.py`). Every draw's key is written by `Key`, so the labels can be
//! changed in one place at step 4.

use std::cell::RefCell;
use std::sync::atomic::{AtomicBool, Ordering};

use pyo3::prelude::*;
use sha2::{Digest, Sha256};

use crate::write_json_string;

/// Whether a thread somewhere keeps its draws, for benchmarks/rule_shadow. Python sees the draws
/// its own rules make; this shows those made in Rust, to compare each with its key and its bits.
static DRAW_LOG_ON: AtomicBool = AtomicBool::new(false);

thread_local! {
    static DRAW_LOG: RefCell<Option<Vec<(String, f64)>>> = const { RefCell::new(None) };
}

/// Keep the draws this thread makes from now on, until `take_draw_log`.
#[pyfunction]
pub(crate) fn start_draw_log() {
    DRAW_LOG_ON.store(true, Ordering::Relaxed);
    DRAW_LOG.with(|log| *log.borrow_mut() = Some(Vec::new()));
}

/// The (key as JSON, value) of each draw this thread made since `start_draw_log`, in order.
#[pyfunction]
pub(crate) fn take_draw_log() -> Vec<(String, f64)> {
    DRAW_LOG.with(|log| log.borrow_mut().take()).unwrap_or_default()
}

fn log_draw(key: &[u8], value: f64) {
    DRAW_LOG.with(|log| {
        if let Some(log) = log.borrow_mut().as_mut() {
            log.push((String::from_utf8_lossy(key).into_owned(), value));
        }
    });
}

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
        let value = u64::from_be_bytes(first) as f64 / 18_446_744_073_709_551_616.0;
        if DRAW_LOG_ON.load(Ordering::Relaxed) {
            log_draw(key, value);
        }
        value
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
