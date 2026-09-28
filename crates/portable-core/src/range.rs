use core::fmt;
use core::ops::Range;

/// The reason a requested range cannot be represented within its capacity.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RangeError {
    /// The requested end exceeds the integer representation.
    Overflow,
    /// The requested end exceeds the available capacity.
    OutOfBounds,
}

impl fmt::Display for RangeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Overflow => "range end overflowed",
            Self::OutOfBounds => "range exceeds capacity",
        })
    }
}

impl core::error::Error for RangeError {}

/// Returns a half-open range after checking its end and available capacity.
///
/// Empty ranges at `capacity` are valid. Empty ranges beyond it are invalid.
/// The returned range must still be applied to a buffer with that capacity.
///
/// # Errors
///
/// Returns [`RangeError::Overflow`] when `start + length` is unrepresentable.
/// Returns [`RangeError::OutOfBounds`] when the end exceeds `capacity`.
pub const fn checked_range(
    start: usize,
    length: usize,
    capacity: usize,
) -> Result<Range<usize>, RangeError> {
    let Some(end) = start.checked_add(length) else {
        return Err(RangeError::Overflow);
    };
    if end > capacity {
        return Err(RangeError::OutOfBounds);
    }
    Ok(start..end)
}

#[cfg(test)]
mod tests;
