//! RunStore — SCHEMA-identical sqlite (port of `sqlite_store.py`, I2).
//! DDL is byte-identical (case + whitespace) so Python list_runs/get_run_log can
//! read Rust-written DBs and vice versa.

use rusqlite::{params, Connection};
use serde_json::{Map, Value as Json};

/// Byte-identical SCHEMA (I2 — case + whitespace match sqlite_store.py).
pub const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    scenario_id   TEXT NOT NULL,
    room_name     TEXT,
    agent_name    TEXT,
    status        TEXT NOT NULL DEFAULT 'running',
    started_utc   TEXT NOT NULL,
    ended_utc     TEXT,
    duration_ms   INTEGER,
    turn_count    INTEGER,
    tool_errors   INTEGER,
    verdict       TEXT,
    report_dir    TEXT,
    summary_json  TEXT
);

CREATE TABLE IF NOT EXISTS run_events (
    run_id        TEXT NOT NULL,
    event_id      TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    turn          INTEGER,
    kind          TEXT NOT NULL,
    ts            INTEGER NOT NULL,
    datetime_utc  TEXT NOT NULL,
    source        TEXT,
    payload_json  TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_run_events_kind ON run_events (run_id, kind);

CREATE TABLE IF NOT EXISTS run_turns (
    run_id          TEXT NOT NULL,
    turn            INTEGER NOT NULL,
    user_text       TEXT,
    agent_text      TEXT,
    turn_taking_ms  INTEGER,
    tool_count      INTEGER,
    tool_errors     INTEGER,
    interrupted     INTEGER,
    PRIMARY KEY (run_id, turn)
);
";

fn opt_str(v: &Json) -> Option<String> {
    match v {
        Json::String(s) => Some(s.clone()),
        _ => None,
    }
}

/// Open a connection + execute SCHEMA (mirror _connect).
pub fn connect(db_path: &str) -> Result<Connection, rusqlite::Error> {
    let db = Connection::open(db_path)?;
    db.execute_batch(SCHEMA)?;
    Ok(db)
}

/// RunStore — sync mirror of sqlite_store.py.
pub struct RunStore {
    db_path: String,
}

impl RunStore {
    pub fn new(db_path: &str) -> Self {
        RunStore {
            db_path: db_path.to_string(),
        }
    }

    pub fn create_run(
        &self,
        run_id: &str,
        scenario_id: &str,
        room_name: &str,
        agent_name: &str,
        started_utc: &str,
        report_dir: &str,
    ) -> Result<(), rusqlite::Error> {
        let db = connect(&self.db_path)?;
        db.execute(
            "INSERT INTO runs (run_id, scenario_id, room_name, agent_name, status, started_utc, report_dir) \
             VALUES (?1, ?2, ?3, ?4, 'running', ?5, ?6)",
            params![run_id, scenario_id, room_name, agent_name, started_utc, report_dir],
        )?;
        Ok(())
    }

    pub fn finish_run(
        &self,
        run_id: &str,
        status: &str,
        summary: &Map<String, Json>,
        ended_utc: &str,
    ) -> Result<(), rusqlite::Error> {
        let db = connect(&self.db_path)?;
        let verdict = summary.get("verdict").and_then(|v| {
            if v.is_null() {
                None
            } else {
                Some(serde_json::to_string(v).unwrap_or_default())
            }
        });
        db.execute(
            "UPDATE runs SET status=?1, ended_utc=?2, duration_ms=?3, turn_count=?4, tool_errors=?5, \
             verdict=?6, summary_json=?7 WHERE run_id=?8",
            params![
                status,
                ended_utc,
                summary.get("duration_ms").and_then(|v| v.as_i64()),
                summary.get("turn_count").and_then(|v| v.as_i64()),
                summary.get("tool_errors").and_then(|v| v.as_i64()),
                verdict,
                serde_json::to_string(summary).unwrap_or_default(),
                run_id
            ],
        )?;
        Ok(())
    }

    pub fn insert_events(
        &self,
        run_id: &str,
        events: &[Map<String, Json>],
    ) -> Result<(), rusqlite::Error> {
        if events.is_empty() {
            return Ok(());
        }
        let db = connect(&self.db_path)?;
        let mut stmt = db.prepare(
            "INSERT OR REPLACE INTO run_events \
             (run_id, event_id, seq, turn, kind, ts, datetime_utc, source, payload_json) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
        )?;
        for e in events {
            let payload = serde_json::to_string(e).unwrap_or_default();
            stmt.execute(params![
                run_id,
                e.get("event_id").and_then(opt_str),
                e.get("seq").and_then(|v| v.as_i64()).unwrap_or(0),
                e.get("turn").and_then(|v| v.as_i64()),
                e.get("kind").and_then(opt_str).unwrap_or_default(),
                e.get("ts").and_then(|v| v.as_i64()).unwrap_or(0),
                e.get("datetime_utc").and_then(opt_str).unwrap_or_default(),
                e.get("source").and_then(opt_str),
                payload,
            ])?;
        }
        Ok(())
    }

    pub fn insert_turns(
        &self,
        run_id: &str,
        turns: &[Map<String, Json>],
    ) -> Result<(), rusqlite::Error> {
        if turns.is_empty() {
            return Ok(());
        }
        let db = connect(&self.db_path)?;
        let mut stmt = db.prepare(
            "INSERT OR REPLACE INTO run_turns \
             (run_id, turn, user_text, agent_text, turn_taking_ms, tool_count, tool_errors, interrupted) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
        )?;
        for t in turns {
            stmt.execute(params![
                run_id,
                t.get("turn").and_then(|v| v.as_i64()).unwrap_or(0),
                t.get("user_text").and_then(opt_str),
                t.get("agent_text").and_then(opt_str),
                t.get("turn_taking_ms").and_then(|v| v.as_i64()),
                t.get("tool_count").and_then(|v| v.as_i64()).unwrap_or(0),
                t.get("tool_errors").and_then(|v| v.as_i64()).unwrap_or(0),
                if t.get("interrupted")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false)
                {
                    1
                } else {
                    0
                },
            ])?;
        }
        Ok(())
    }

    /// Convert a SQLite row to a JSON map with type fidelity — INTEGER columns
    /// stay numbers (Python sqlite3 preserves ints; reading as strings was a
    /// parity bug in get_run_status/list_runs/compare). Probes int → float →
    /// string since rusqlite 0.40 has no public row column_type.
    fn row_to_json(row: &rusqlite::Row<'_>) -> Map<String, Json> {
        let stmt = row.as_ref();
        let mut m = Map::new();
        for i in 0..stmt.column_count() {
            let name = stmt.column_name(i).unwrap_or("").to_string();
            let val = if let Ok(Some(n)) = row.get::<_, Option<i64>>(i) {
                Json::Number(n.into())
            } else if let Ok(Some(f)) = row.get::<_, Option<f64>>(i) {
                Json::from(f)
            } else if let Ok(Some(s)) = row.get::<_, Option<String>>(i) {
                Json::String(s)
            } else {
                Json::Null
            };
            m.insert(name, val);
        }
        m
    }

    pub fn get_run(&self, run_id: &str) -> Result<Option<Map<String, Json>>, rusqlite::Error> {
        let db = connect(&self.db_path)?;
        let mut stmt = db.prepare("SELECT * FROM runs WHERE run_id=?1")?;
        let mut rows = stmt.query(params![run_id])?;
        if let Some(row) = rows.next()? {
            Ok(Some(Self::row_to_json(row)))
        } else {
            Ok(None)
        }
    }

    pub fn list_runs(
        &self,
        limit: i64,
        scenario_id: Option<&str>,
    ) -> Result<Vec<Map<String, Json>>, rusqlite::Error> {
        let db = connect(&self.db_path)?;
        let mut out = Vec::new();
        // Python sqlite_store.list_runs selects EXACTLY these columns (no
        // ended_utc/report_dir/summary_json) — shape parity for list_runs.
        let cols = "run_id, scenario_id, room_name, agent_name, status, started_utc, duration_ms, turn_count, tool_errors, verdict";
        let mut stmt = if scenario_id.is_some() {
            db.prepare(&format!(
                "SELECT {cols} FROM runs WHERE scenario_id=?1 ORDER BY started_utc DESC LIMIT ?2"
            ))?
        } else {
            db.prepare(&format!(
                "SELECT {cols} FROM runs ORDER BY started_utc DESC LIMIT ?1"
            ))?
        };
        let mut rows = if let Some(sid) = scenario_id {
            stmt.query(params![sid, limit])?
        } else {
            stmt.query(params![limit])?
        };
        while let Some(row) = rows.next()? {
            out.push(Self::row_to_json(row));
        }
        Ok(out)
    }
}
