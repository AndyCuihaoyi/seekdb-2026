//! Temporary instrumentation: cumulative wall clock per phase, printed to stderr.
//!
//! Enabled with MINIVECDB_PROFILE=1. This file is removed once the profile
//! has been written.

use std::cell::Cell;
use std::time::Instant;

thread_local! {
    static NAMES: std::cell::RefCell<Vec<&'static str>> = const { std::cell::RefCell::new(Vec::new()) };
    static ACC: std::cell::RefCell<Vec<(u128, u64)>> = const { std::cell::RefCell::new(Vec::new()) };
    static ON: Cell<bool> = const { Cell::new(false) };
}

pub fn enable() {
    ON.with(|o| o.set(std::env::var("MINIVECDB_PROFILE").is_ok()));
}

pub fn on() -> bool {
    ON.with(|o| o.get())
}

pub fn add(name: &'static str, ns: u128) {
    NAMES.with(|n| {
        let mut n = n.borrow_mut();
        if !n.contains(&name) {
            n.push(name);
            ACC.with(|a| a.borrow_mut().push((0, 0)));
        }
    });
    ACC.with(|a| {
        let mut a = a.borrow_mut();
        let mut idx = None;
        NAMES.with(|n| {
            let n = n.borrow();
            for (i, x) in n.iter().enumerate() {
                if *x == name {
                    idx = Some(i);
                    break;
                }
            }
        });
        if let Some(i) = idx {
            a[i].0 += ns;
            a[i].1 += 1;
        }
    });
}

pub fn measure<T>(name: &'static str, f: impl FnOnce() -> T) -> T {
    if !on() {
        return f();
    }
    let t0 = Instant::now();
    let r = f();
    add(name, t0.elapsed().as_nanos());
    r
}

pub fn dump() {
    if !on() {
        return;
    }
    NAMES.with(|n| {
        let n = n.borrow();
        ACC.with(|a| {
            let a = a.borrow();
            let mut out = String::new();
            for (i, name) in n.iter().enumerate() {
                out.push_str(&format!(
                    "PROF {:<28} {:>10.3} ms  x{}\n",
                    name,
                    a[i].0 as f64 / 1e6,
                    a[i].1
                ));
            }
            eprint!("{}", out);
        });
    });
}
