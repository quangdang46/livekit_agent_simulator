//! lks-core — pure logic for livekit-agent-simulator.
//!
//! This crate intentionally has NO livekit / pyo3 dependency. The fine-grained
//! workspace split (D1) exists so config/scenario (P0/P1) build and test without
//! building libwebrtc — `livekit` pulls a C++ libwebrtc build that only enters
//! via the `lks-livekit` crate from P2 onward.
//!
//! Modules land phase by phase per the port plan (P0: errors only; P1: config,
//! scenario, paths, authoring; P3: logging/event/sqlite/summary/meta, metrics,
//! script farewell/hang_up_gate/summary; P4: asserts, suite, scenario_from_dict;
//! P5: scenario_from_run; P7: evals, judge; P9: optimize).

pub mod errors;

pub mod config;

pub mod asserts;
pub mod authoring;
pub mod behavior_compile;
pub mod caller_policy;
pub mod persona_traits;
pub mod prompt_sections;
pub mod scenario;
pub mod scenario_jsonl;
pub mod scenario_ops;
pub mod scenario_yaml;
pub mod script;
pub mod yaml_writer;
