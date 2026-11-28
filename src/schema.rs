//! JSON Schema checks of the JSON that comes from outside Whooper's code: a review agent's findings and
//! overview, a poll's answer, a settings form's values. The jsonschema crate validates; this words its
//! first error the way Whooper's messages name a place, such as findings.json.findings[0].

use jsonschema::error::ValidationErrorKind;
use serde_json::Value;

/// Ok when value fits schema, and otherwise the first place it breaks it, in words, starting with
/// where, the name of the value, such as findings.json.
pub fn check(value: &Value, schema: &Value, place: &str) -> Result<(), String> {
    let validator = match jsonschema::validator_for(schema) {
        Ok(validator) => validator,
        Err(e) => return Err(format!("the schema is not valid: {e}")),
    };
    let errors: Vec<_> = validator.iter_errors(value).collect();
    // The shallowest error first, as a reader fixes the outer object before its parts, and of those a
    // missing property first.
    let first = errors.iter().min_by_key(|e| {
        let depth = e.instance_path().into_iter().count();
        let rank = match e.kind() {
            ValidationErrorKind::Type { .. } => 0,
            ValidationErrorKind::Required { .. } => 1,
            _ => 2,
        };
        (depth, rank)
    });
    let Some(error) = first else { return Ok(()) };
    let mut at = place.to_string();
    let mut instance = value;
    for segment in error.instance_path().into_iter() {
        let key = segment.to_string();
        if let Ok(i) = key.parse::<usize>()
            && instance.is_array()
        {
            at.push_str(&format!("[{i}]"));
            instance = &instance[i];
            continue;
        }
        at.push_str(&format!(".{key}"));
        instance = &instance[key.as_str()];
    }
    let words = match error.kind() {
        ValidationErrorKind::Required { .. } => {
            // Every required property missing at this place, in the schema's order.
            let missing: Vec<String> = errors
                .iter()
                .filter(|e| e.instance_path() == error.instance_path())
                .filter_map(|e| match e.kind() {
                    ValidationErrorKind::Required { property } => {
                        property.as_str().map(String::from)
                    }
                    _ => None,
                })
                .collect();
            format!("{at} lacks {}", missing.join(", "))
        }
        ValidationErrorKind::AdditionalProperties { unexpected } => {
            let mut extra = unexpected.clone();
            extra.sort();
            format!("{at} has unexpected {}", extra.join(", "))
        }
        ValidationErrorKind::Type { kind } => {
            let names: Vec<String> = match kind {
                jsonschema::error::TypeKind::Single(t) => vec![t.to_string()],
                jsonschema::error::TypeKind::Multiple(ts) => {
                    ts.iter().map(|t| t.to_string()).collect()
                }
            };
            format!("{at} is not of type {}", names.join(" or "))
        }
        ValidationErrorKind::Enum { options } => {
            format!("{at} is {instance}, not one of {options}")
        }
        ValidationErrorKind::Constant { expected_value } => {
            format!("{at} is {instance}, not {expected_value}")
        }
        ValidationErrorKind::MinLength { limit } => {
            format!("{at} is shorter than {limit} characters")
        }
        ValidationErrorKind::Minimum { limit } => format!("{at} is below {limit}"),
        ValidationErrorKind::ExclusiveMinimum { limit } => format!("{at} is not above {limit}"),
        ValidationErrorKind::Maximum { limit } => format!("{at} is above {limit}"),
        ValidationErrorKind::ExclusiveMaximum { limit } => format!("{at} is not below {limit}"),
        ValidationErrorKind::Pattern { pattern } => {
            format!("{at} is {instance}, which does not match {pattern}")
        }
        ValidationErrorKind::MinItems { limit } => format!("{at} has fewer than {limit} items"),
        ValidationErrorKind::MaxItems { limit } => format!("{at} has more than {limit} items"),
        ValidationErrorKind::UniqueItems => format!("{at} holds an item twice"),
        _ => format!("{at}: {error}"),
    };
    Err(words)
}
