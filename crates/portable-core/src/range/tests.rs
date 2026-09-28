use super::{RangeError, checked_range};

#[test]
fn accepts_interior_range() {
    assert_eq!(checked_range(2, 3, 8), Ok(2..5), "interior range must fit");
}

#[test]
fn accepts_exact_capacity() {
    assert_eq!(checked_range(0, 8, 8), Ok(0..8), "full range must fit");
}

#[test]
fn accepts_empty_range_at_end() {
    assert_eq!(checked_range(8, 0, 8), Ok(8..8), "empty end range is valid");
}

#[test]
fn accepts_empty_capacity() {
    assert_eq!(checked_range(0, 0, 0), Ok(0..0), "empty capacity is valid");
}

#[test]
fn rejects_end_beyond_capacity() {
    assert_eq!(
        checked_range(6, 3, 8),
        Err(RangeError::OutOfBounds),
        "out-of-bounds input must remain an error"
    );
}

#[test]
fn rejects_empty_range_beyond_capacity() {
    assert_eq!(
        checked_range(9, 0, 8),
        Err(RangeError::OutOfBounds),
        "zero length must not bypass bounds validation"
    );
}

#[test]
fn rejects_integer_overflow() {
    assert_eq!(
        checked_range(usize::MAX, 1, usize::MAX),
        Err(RangeError::Overflow),
        "overflow must not wrap into an apparently valid range"
    );
}

#[test]
fn accepts_largest_representable_empty_range() {
    assert_eq!(
        checked_range(usize::MAX, 0, usize::MAX),
        Ok(usize::MAX..usize::MAX),
        "a representable empty range must not overflow"
    );
}
