//! What every lane shares: JSON values, float operations with CPython's results, keyed draws
//! and interned singletons (docs/rust-port/lanes/p3-skeleton.md).

pub(crate) mod interner;
pub(crate) mod mathx;
pub(crate) mod rng;
pub(crate) mod tracked;
pub(crate) mod value;
pub(crate) mod world_core;
