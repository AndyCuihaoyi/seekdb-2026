//! The switch of the diagnostics.
//!
//! The database prints what it measures to the standard error, so that a
//! change of the index or of the caches is visible. It does so **only when
//! it is asked to**, through the environment variable `MINIVECDB_DIAG`.
//!
//! The reason is a measurement, not a taste. A write to the standard
//! error is a synchronous write to a file descriptor: it costs a system
//! call and, when the descriptor is a pipe, it can block until the reader
//! drains it. The time of the queries is two thirds of the weight of the
//! score, so a thousand queries that each print a line would pay a
//! thousand synchronous writes for nothing.
//!
//! There is a second reason, and it is the one that made this module
//! necessary. A caller that reads the standard error of the database
//! through a pipe and does not drain it - which is what
//! `tools/check_ann_nprobe_equivalence.py` does - will block the database
//! as soon as the pipe is full, and the database stops answering. The
//! switch makes the silence the default, so that a caller that does not
//! read the diagnostics is not affected by them at all.

use std::sync::OnceLock;

static ENABLED: OnceLock<bool> = OnceLock::new();

/// Whether the diagnostics are to be printed.
///
/// The value of the environment variable is read once, on the first call,
/// so that the check costs nothing per query.
pub fn enabled() -> bool {
    *ENABLED.get_or_init(|| {
        std::env::var("MINIVECDB_DIAG")
            .map(|v| {
                let v = v.trim().to_ascii_lowercase();
                !(v.is_empty() || v == "0" || v == "false" || v == "no" || v == "off")
            })
            .unwrap_or(false)
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_switch_is_off_without_the_variable() {
        // The variable is read once for the whole process, so the test
        // only asserts the rule and not the state of the environment.
        let value = std::env::var("MINIVECDB_DIAG").ok();
        assert!(!enabled() || value.is_some());
    }
}
