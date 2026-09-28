//! Public API integration tests. Helpers share the native Clippy test exceptions.
#![cfg(test)]
#![forbid(unsafe_code)]

use portable_core::{RangeError, checked_range};

#[test]
fn checked_range_selects_the_requested_bytes() {
    let bytes = b"abcd";
    let range = checked_range(1, 2, bytes.len()).expect("the requested range must fit");
    assert_eq!(&bytes[range], b"bc", "range must select the middle bytes");
}

#[test]
fn error_implements_the_core_error_contract() {
    let error: &dyn core::error::Error = &RangeError::Overflow;
    assert!(
        error.source().is_none(),
        "range errors have no source error"
    );
}
