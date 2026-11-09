//! Storage layer: WAL, row files, buffer pool and recovery.
//!
//! The buffer pool and the dimension-major block reader are wired in during
//! the performance stage; they are complete and unit tested already.

#![allow(dead_code)]

pub mod bufferpool;
pub mod format;
pub mod table;
pub mod wal;
