//! Allocation-free range validation with explicit overflow and bounds errors.
//!
//! ```
//! use portable_core::checked_range;
//!
//! let range = checked_range(2, 3, 8)?;
//! assert_eq!(range, 2..5, "the range must cover three elements");
//! # Ok::<(), portable_core::RangeError>(())
//! ```

#![cfg_attr(not(feature = "std"), no_std)]
#![cfg_attr(not(feature = "unsafe-code"), forbid(unsafe_code))]

mod range;

pub use range::{RangeError, checked_range};
