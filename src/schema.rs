//! JSON Schema checks of the JSON that comes from outside Whooper's code: a review agent's findings and
//! overview, a poll's answer, a settings form's values. The jsonschema crate validates; this words its
//! first error the way Whooper's messages name a place, such as findings.json.findings[0].

use jsonschema::error::ValidationErrorKind;
use serde_json::Value;

/// JSON Schema counts 12.0 as an integer, but the code that reads a whole number, such as a finding's
/// line, takes only one written without a fraction, so check gives each place the schema types integer
/// this keyword too, which refuses a number written with one.
const WHOLE: &str = "wholeNumber";

struct WholeNumber;

impl<'i> jsonschema::Keyword<'i> for WholeNumber {
    fn validate(&self, instance: &'i Value) -> Result<(), jsonschema::ValidationError<'i>> {
        if self.is_valid(instance) {
            Ok(())
        } else {
            Err(jsonschema::ValidationError::custom(
                "is not of type integer",
            ))
        }
    }

    fn is_valid(&self, instance: &'i Value) -> bool {
        !instance.is_f64()
    }
}

/// Adds WHOLE to each subschema whose type takes integers but not other numbers.
fn mark_integers(schema: &mut Value) {
    match schema {
        Value::Object(map) => {
            let types: Vec<&str> = match map.get("type") {
                Some(Value::String(t)) => vec![t.as_str()],
                Some(Value::Array(ts)) => ts.iter().filter_map(Value::as_str).collect(),
                _ => vec![],
            };
            let integer = types.contains(&"integer") && !types.contains(&"number");
            map.values_mut().for_each(mark_integers);
            if integer {
                map.insert(WHOLE.into(), Value::Bool(true));
            }
        }
        Value::Array(list) => list.iter_mut().for_each(mark_integers),
        _ => {}
    }
}

/// Ok when value fits schema, and otherwise the first place it breaks it, in words, starting with
/// where, the name of the value, such as findings.json.
pub fn check(value: &Value, schema: &Value, place: &str) -> Result<(), String> {
    let mut schema = schema.clone();
    mark_integers(&mut schema);
    let validator = match jsonschema::options()
        .with_keyword(WHOLE, |_, _, _| Ok(Box::new(WholeNumber)))
        .build(&schema)
    {
        Ok(validator) => validator,
        Err(e) => return Err(format!("the schema is not valid: {e}")),
    };
    let errors: Vec<_> = validator.iter_errors(value).collect();
    // The shallowest error first, as a reader fixes the outer object before its parts, and of those a
    // missing property first.
    let first = errors.iter().min_by_key(|e| {
        let depth = e.instance_path().into_iter().count();
        let rank = match e.kind() {
            ValidationErrorKind::Type { .. } | ValidationErrorKind::Custom { .. } => 0,
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
        ValidationErrorKind::Custom { keyword, .. } if keyword == WHOLE => {
            format!("{at} is not of type integer")
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

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn schema(name: &str) -> Value {
        let path = crate::config::root().join("schemas").join(name);
        serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
    }

    fn finding() -> Value {
        json!({"id": "1", "path": "services/orders/internal/api/orders.go", "line": 88, "side": "RIGHT",
               "in_diff": true, "title": "Bad JSON now returns 500", "draft": "**issue:** ...",
               "kind": "caller_visible_bug"})
    }

    fn result() -> Value {
        json!({"status": "reviewed", "skip_reason": "", "findings": [finding()],
               "approval": {"verdict": "look_first", "reason": "Ran the api tests; no database path."}})
    }

    #[test]
    fn findings_that_fit_pass() {
        check(&result(), &schema("findings.json"), "findings.json").unwrap();
    }

    #[test]
    fn the_findings_hold_no_overview() {
        // The overview is a file of its own, overview.json, which cannot fail the review.
        assert!(
            schema("findings.json")["properties"]
                .get("overview")
                .is_none()
        );
    }

    #[test]
    fn each_break_of_the_findings_is_caught() {
        let with_finding = |key: &str, value: Value| {
            let mut r = result();
            r["findings"][0][key] = value;
            r
        };
        let mut no_approval = result();
        no_approval.as_object_mut().unwrap().remove("approval");
        let mut other_status = result();
        other_status["status"] = json!("done");
        let mut extra = result();
        extra["extra"] = json!("x");
        for (name, value, words) in [
            (
                "a required key is missing",
                no_approval,
                "findings.json lacks approval",
            ),
            (
                "a value outside its enum",
                other_status,
                r#"findings.json.status is "done", not one of ["reviewed","skipped"]"#,
            ),
            (
                "an unexpected key",
                extra,
                "findings.json has unexpected extra",
            ),
            (
                "a string where an integer belongs",
                with_finding("line", json!("88")),
                "findings.json.findings[0].line is not of type integer",
            ),
            (
                "true where an integer belongs",
                with_finding("line", json!(true)),
                "findings.json.findings[0].line is not of type integer",
            ),
            (
                "an integer below its minimum",
                with_finding("line", json!(0)),
                "findings.json.findings[0].line is below 1",
            ),
            (
                "an integer where a boolean belongs",
                with_finding("in_diff", json!(1)),
                "findings.json.findings[0].in_diff is not of type boolean",
            ),
        ] {
            assert_eq!(
                check(&value, &schema("findings.json"), "findings.json"),
                Err(words.to_string()),
                "{name}"
            );
        }
    }

    #[test]
    fn a_float_with_no_fraction_where_an_integer_belongs_is_refused() {
        // JSON Schema counts 12.0 as an integer, but the code that reads a finding's line takes an int.
        let mut r = result();
        r["findings"][0]["line"] = json!(12.0);
        assert_eq!(
            check(&r, &schema("findings.json"), "findings.json"),
            Err("findings.json.findings[0].line is not of type integer".to_string())
        );
        // A number that may have a fraction still takes one written with it.
        check(&json!(10.0), &json!({"type": "number"}), "n").unwrap();
        check(&json!(10.0), &json!({"type": ["integer", "number"]}), "n").unwrap();
    }

    #[test]
    fn it_checks_the_keywords_of_prompting_times_status_schema() {
        let schema = schema("prompting-time-status.json");
        let item = json!({"id": "r1", "group": "look", "title": "T", "detail": null, "dot": "attention",
            "project": null, "updated_at": "2031-07-30T15:00:00-07:00", "route": "/reviews/r1",
            "actions": ["skip"]});
        let status = json!({"format": 2, "agent": {"words": "Running", "dot": "idle"}, "blockers": [],
            "items": [item], "truncated": false});
        check(&status, &schema, "status").unwrap();
        let with_item = |key: &str, value: Value| {
            let mut i = item.clone();
            i[key] = value;
            json!([i])
        };
        for (name, key, value) in [
            ("another format, by const", "format", json!(1)),
            ("true for the format", "format", json!(true)),
            (
                "a dot outside the $defs enum",
                "agent",
                json!({"words": "Running", "dot": "red"}),
            ),
            (
                "an empty id, below minLength",
                "items",
                with_item("id", json!("")),
            ),
            (
                "a route that is not a path, by the $defs pattern",
                "items",
                with_item("route", json!("reviews/r1")),
            ),
            (
                "a row action twice, against uniqueItems",
                "items",
                with_item("actions", json!(["skip", "skip"])),
            ),
            ("more than 50 items", "items", json!(vec![item.clone(); 51])),
        ] {
            let mut broken = status.clone();
            broken[key] = value;
            assert!(check(&broken, &schema, "status").is_err(), "{name}");
        }
    }

    #[test]
    fn an_exclusive_minimum_takes_only_numbers_above_it() {
        // schemas/settings.json takes an [ask] limit of any number of minutes above 0.
        let schema = json!({"type": "number", "exclusiveMinimum": 0});
        for value in [json!(0.05), json!(2.5), json!(10)] {
            check(&value, &schema, "settings.ask.timeout_minutes").unwrap();
        }
        for value in [json!(0), json!(0.0), json!(-1)] {
            assert_eq!(
                check(&value, &schema, "settings.ask.timeout_minutes"),
                Err("settings.ask.timeout_minutes is not above 0".to_string())
            );
        }
    }

    #[test]
    fn the_plugin_manifest_fits_its_schema_and_the_schema_catches_each_break() {
        let manifest: Value = serde_json::from_str(
            &std::fs::read_to_string(crate::config::root().join("prompting-time-plugin.json"))
                .unwrap(),
        )
        .unwrap();
        let schema = schema("prompting-time-plugin.json");
        check(&manifest, &schema, "manifest").unwrap();
        let unflagged: Vec<Value> = manifest["actions"]
            .as_array()
            .unwrap()
            .iter()
            .map(|a| {
                let mut a = a.clone();
                a.as_object_mut().unwrap().remove("confirm");
                a
            })
            .collect();
        let mut format_1 = manifest["status"].clone();
        format_1["format"] = json!(1);
        let mut upper_group = manifest["status"].clone();
        upper_group["groups"] =
            json!([{"id": "Ready", "title": "Ready", "needs_you": true, "order": 1}]);
        for (name, key, value) in [
            (
                "a name a pt-plugin:// address cannot hold",
                "name",
                json!("Whooper Reviews"),
            ),
            (
                "a version that is not <major>.<minor>.<patch>",
                "version",
                json!("v0.1"),
            ),
            ("an empty start command", "start", json!([])),
            (
                "actions without their confirm flag",
                "actions",
                json!(unflagged),
            ),
            (
                "a page protocol the format does not know",
                "page",
                json!({"entry": "page/dist/index.html", "protocol": 3}),
            ),
            (
                "a status format Prompting Time does not read",
                "status",
                format_1,
            ),
            (
                "a status group whose id is not lowercase",
                "status",
                upper_group,
            ),
            ("an unexpected key", "socket", json!("whooper.sock")),
        ] {
            let mut broken = manifest.clone();
            broken[key] = value;
            assert!(check(&broken, &schema, "manifest").is_err(), "{name}");
        }
        let mut no_page = manifest.clone();
        no_page.as_object_mut().unwrap().remove("page");
        assert!(check(&no_page, &schema, "manifest").is_err());
    }
}
