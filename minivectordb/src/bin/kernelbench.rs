// Micro-benchmark of the distance kernel.
//
// The question it answers: is the exact scan limited by the arithmetic of
// the kernel, by the memory traffic of streaming the vectors, or by the
// cost of the per-row overhead?
//
//   cargo run --release --bin kernelbench

use std::time::Instant;

#[cfg(target_arch = "x86_64")]
use std::arch::x86_64::*;

/// The kernel as it is in the code today: mul and add, four accumulators.
#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn l2_sq_avx2(a: &[f32], b: &[f32]) -> f32 {
    let n = a.len();
    let mut acc0 = _mm256_setzero_ps();
    let mut acc1 = _mm256_setzero_ps();
    let mut acc2 = _mm256_setzero_ps();
    let mut acc3 = _mm256_setzero_ps();
    let mut i = 0usize;
    while i + 32 <= n {
        let d0 = _mm256_sub_ps(_mm256_loadu_ps(a.as_ptr().add(i)), _mm256_loadu_ps(b.as_ptr().add(i)));
        let d1 = _mm256_sub_ps(_mm256_loadu_ps(a.as_ptr().add(i + 8)), _mm256_loadu_ps(b.as_ptr().add(i + 8)));
        let d2 = _mm256_sub_ps(_mm256_loadu_ps(a.as_ptr().add(i + 16)), _mm256_loadu_ps(b.as_ptr().add(i + 16)));
        let d3 = _mm256_sub_ps(_mm256_loadu_ps(a.as_ptr().add(i + 24)), _mm256_loadu_ps(b.as_ptr().add(i + 24)));
        acc0 = _mm256_add_ps(acc0, _mm256_mul_ps(d0, d0));
        acc1 = _mm256_add_ps(acc1, _mm256_mul_ps(d1, d1));
        acc2 = _mm256_add_ps(acc2, _mm256_mul_ps(d2, d2));
        acc3 = _mm256_add_ps(acc3, _mm256_mul_ps(d3, d3));
        i += 32;
    }
    let mut acc = _mm256_add_ps(_mm256_add_ps(acc0, acc1), _mm256_add_ps(acc2, acc3));
    while i + 8 <= n {
        let d = _mm256_sub_ps(_mm256_loadu_ps(a.as_ptr().add(i)), _mm256_loadu_ps(b.as_ptr().add(i)));
        acc = _mm256_add_ps(acc, _mm256_mul_ps(d, d));
        i += 8;
    }
    let lo = _mm256_castps256_ps128(acc);
    let hi = _mm256_extractf128_ps(acc, 1);
    let s = _mm_add_ps(lo, hi);
    let s = _mm_add_ps(s, _mm_movehl_ps(s, s));
    let s = _mm_add_ss(s, _mm_shuffle_ps(s, s, 0x55));
    let mut sum = _mm_cvtss_f32(s);
    while i < n {
        let d = a[i] - b[i];
        sum += d * d;
        i += 1;
    }
    sum
}

/// The same kernel with the fused multiply-add.
#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2,fma")]
unsafe fn l2_sq_avx2_fma(a: &[f32], b: &[f32]) -> f32 {
    let n = a.len();
    let mut acc0 = _mm256_setzero_ps();
    let mut acc1 = _mm256_setzero_ps();
    let mut acc2 = _mm256_setzero_ps();
    let mut acc3 = _mm256_setzero_ps();
    let mut i = 0usize;
    while i + 32 <= n {
        let d0 = _mm256_sub_ps(_mm256_loadu_ps(a.as_ptr().add(i)), _mm256_loadu_ps(b.as_ptr().add(i)));
        let d1 = _mm256_sub_ps(_mm256_loadu_ps(a.as_ptr().add(i + 8)), _mm256_loadu_ps(b.as_ptr().add(i + 8)));
        let d2 = _mm256_sub_ps(_mm256_loadu_ps(a.as_ptr().add(i + 16)), _mm256_loadu_ps(b.as_ptr().add(i + 16)));
        let d3 = _mm256_sub_ps(_mm256_loadu_ps(a.as_ptr().add(i + 24)), _mm256_loadu_ps(b.as_ptr().add(i + 24)));
        acc0 = _mm256_fmadd_ps(d0, d0, acc0);
        acc1 = _mm256_fmadd_ps(d1, d1, acc1);
        acc2 = _mm256_fmadd_ps(d2, d2, acc2);
        acc3 = _mm256_fmadd_ps(d3, d3, acc3);
        i += 32;
    }
    let mut acc = _mm256_add_ps(_mm256_add_ps(acc0, acc1), _mm256_add_ps(acc2, acc3));
    while i + 8 <= n {
        let d = _mm256_sub_ps(_mm256_loadu_ps(a.as_ptr().add(i)), _mm256_loadu_ps(b.as_ptr().add(i)));
        acc = _mm256_fmadd_ps(d, d, acc);
        i += 8;
    }
    let lo = _mm256_castps256_ps128(acc);
    let hi = _mm256_extractf128_ps(acc, 1);
    let s = _mm_add_ps(lo, hi);
    let s = _mm_add_ps(s, _mm_movehl_ps(s, s));
    let s = _mm_add_ss(s, _mm_shuffle_ps(s, s, 0x55));
    let mut sum = _mm_cvtss_f32(s);
    while i < n {
        let d = a[i] - b[i];
        sum += d * d;
        i += 1;
    }
    sum
}

/// The kernel with eight accumulators, to see whether the dependency chain
/// or the width of the machine is the limit.
#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn l2_sq_avx2_wide(a: &[f32], b: &[f32]) -> f32 {
    let n = a.len();
    let mut acc = [_mm256_setzero_ps(); 8];
    let mut i = 0usize;
    while i + 64 <= n {
        for k in 0..8 {
            let d = _mm256_sub_ps(
                _mm256_loadu_ps(a.as_ptr().add(i + k * 8)),
                _mm256_loadu_ps(b.as_ptr().add(i + k * 8)),
            );
            acc[k] = _mm256_add_ps(acc[k], _mm256_mul_ps(d, d));
        }
        i += 64;
    }
    let mut a0 = _mm256_add_ps(_mm256_add_ps(acc[0], acc[1]), _mm256_add_ps(acc[2], acc[3]));
    let a1 = _mm256_add_ps(_mm256_add_ps(acc[4], acc[5]), _mm256_add_ps(acc[6], acc[7]));
    let mut acc = _mm256_add_ps(a0, a1);
    while i + 8 <= n {
        let d = _mm256_sub_ps(_mm256_loadu_ps(a.as_ptr().add(i)), _mm256_loadu_ps(b.as_ptr().add(i)));
        acc = _mm256_add_ps(acc, _mm256_mul_ps(d, d));
        i += 8;
    }
    let lo = _mm256_castps256_ps128(acc);
    let hi = _mm256_extractf128_ps(acc, 1);
    let s = _mm_add_ps(lo, hi);
    let s = _mm_add_ps(s, _mm_movehl_ps(s, s));
    let s = _mm_add_ss(s, _mm_shuffle_ps(s, s, 0x55));
    let mut sum = _mm_cvtss_f32(s);
    while i < n {
        let d = a[i] - b[i];
        sum += d * d;
        i += 1;
    }
    sum
}

fn fill(state: &mut u64, out: &mut [f32]) {
    for v in out.iter_mut() {
        *state ^= *state << 13;
        *state ^= *state >> 7;
        *state ^= *state << 17;
        *v = (*state >> 40) as f32 / 16_777_216.0;
    }
}

fn bench(name: &str, f: impl Fn(&[f32], &[f32]) -> f32, matrix: &[f32], query: &[f32], rows: usize, dim: usize, queries: usize) {
    let mut best = f64::MAX;
    let mut acc = 0.0f32;
    for _ in 0..3 {
        let t0 = Instant::now();
        for _ in 0..queries {
            for r in 0..rows {
                acc += f(&matrix[r * dim..(r + 1) * dim], query);
            }
        }
        let dt = t0.elapsed().as_secs_f64();
        if dt < best {
            best = dt;
        }
    }
    let bytes = (rows * dim * 4) as f64;
    println!(
        "  {:<28} {:8.1} ms  {:6.2} GB/s  {:5.1} ns/row  (acc {:.4})",
        name,
        best * 1000.0,
        (bytes * queries as f64) / best / 1e9,
        best * 1e9 / (queries * rows) as f64,
        acc
    );
}

fn main() {
    let dim = 128usize;
    let queries = 1000usize;
    let mut state = 0x243f_6a88_85a3_08d3u64;
    let mut query = vec![0.0f32; dim];
    fill(&mut state, &mut query);

    for rows in [10000usize, 2000, 500] {
        let mut matrix = vec![0.0f32; rows * dim];
        fill(&mut state, &mut matrix);
        println!("{} rows x {} dims = {:.2} MiB", rows, dim, (rows * dim * 4) as f64 / 1048576.0);
        bench("avx2 (today)", |a, b| unsafe { l2_sq_avx2(a, b) }, &matrix, &query, rows, dim, queries);
        if std::is_x86_feature_detected!("fma") {
            bench("avx2 + fma", |a, b| unsafe { l2_sq_avx2_fma(a, b) }, &matrix, &query, rows, dim, queries);
        }
        bench("avx2, 8 accumulators", |a, b| unsafe { l2_sq_avx2_wide(a, b) }, &matrix, &query, rows, dim, queries);
        println!();
    }
}
