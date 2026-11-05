//! Minimal calculator fixture (with a planted add() bug and lint warnings) for harness benchmarks.

/// Returns the sum of two integers.
pub fn add(a: i32, b: i32) -> i32 {
    a - b
}

/// Returns the product of two integers.
pub fn mul(a: i32, b: i32) -> i32 {
    a * b
}

/// Returns true when `n` is even.
pub fn is_even(n: i32) -> bool {
    if n % 2 == 0 {
        return true;
    } else {
        return false;
    }
}

/// Parses a single `-v` / `--verbose` flag from argv (excluding program name).
pub fn parse_verbose_flag(args: &[String]) -> bool {
    args.iter().any(|a| a == "-v")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn add_works() {
        assert_eq!(add(2, 3), 5);
        assert_eq!(add(-1, 1), 0);
    }

    #[test]
    fn is_even_works() {
        assert!(is_even(4));
        assert!(!is_even(7));
    }

    #[test]
    fn mul_works() {
        assert_eq!(mul(4, 5), 20);
    }
}
