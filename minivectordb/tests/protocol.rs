//! The protocol level tests of MiniVectorDB.
//!
//! The tests drive the binary over its standard streams, the way the judge
//! does. They cover the parts of the contract that the SQL level tests
//! cannot reach: the handshake, the shape of the responses, the handling of
//! a malformed request and the shutdown.

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

/// A database process under test.
struct Server {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

impl Server {
    /// Start the server on a fresh data directory.
    ///
    /// The handshake is read here, because it arrives before the first
    /// request and the caller has to see it.
    fn start(data_dir: &Path) -> (Server, String) {
        let mut child = Command::new(env!("CARGO_BIN_EXE_minivectordb"))
            .arg("--data-dir")
            .arg(data_dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("the database starts");
        let stdin = child.stdin.take().expect("the standard input is piped");
        let stdout = BufReader::new(child.stdout.take().expect("the standard output is piped"));
        let mut server = Server { child, stdin, stdout };
        let handshake = server.read_line();
        (server, handshake)
    }

    /// Read one line of the output.
    fn read_line(&mut self) -> String {
        let mut line = String::new();
        let n = self.stdout.read_line(&mut line).expect("the output is readable");
        assert!(n > 0, "the database closed its output");
        line.trim_end().to_string()
    }

    /// Send a request and return the answer.
    fn request(&mut self, id: u32, op: &str, sql: Option<&str>, mode: &str) -> String {
        let mut request = format!("{{\"id\":{id},\"op\":\"{op}\"");
        if let Some(sql) = sql {
            request.push_str(&format!(",\"sql\":{}", quote(sql)));
            request.push_str(&format!(",\"search_mode\":\"{mode}\""));
        }
        request.push('}');
        writeln!(self.stdin, "{request}").expect("the request is written");
        self.stdin.flush().expect("the request is flushed");
        self.read_line()
    }

    /// Send a raw line and return the answer.
    fn raw(&mut self, line: &str) -> String {
        writeln!(self.stdin, "{line}").expect("the line is written");
        self.stdin.flush().expect("the line is flushed");
        self.read_line()
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Quote a string for JSON.
fn quote(text: &str) -> String {
    let mut out = String::from("\"");
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// A temporary directory that removes itself when the test ends.
struct TempDir(PathBuf);

impl TempDir {
    fn new(tag: &str) -> TempDir {
        let mut path = std::env::temp_dir();
        path.push(format!("mvdb-proto-test-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&path);
        std::fs::create_dir_all(&path).unwrap();
        TempDir(path)
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[test]
fn the_handshake_is_the_first_line() {
    let dir = TempDir::new("handshake");
    let (_server, handshake) = Server::start(&dir.0);
    assert_eq!(handshake, r#"{"type":"ready"}"#);
}

#[test]
fn a_query_answers_with_the_columns_and_the_rows() {
    let dir = TempDir::new("query");
    let (mut server, _handshake) = Server::start(&dir.0);
    let answer = server.request(
        1,
        "execute",
        Some("CREATE TABLE docs (id BIGINT PRIMARY KEY, content VARCHAR(16));"),
        "exact",
    );
    assert!(answer.contains(r#""ok":true"#), "{answer}");
    let answer = server.request(
        2,
        "execute",
        Some("INSERT INTO docs VALUES (1, '你好');"),
        "exact",
    );
    assert!(answer.contains(r#""affected_rows":1"#), "{answer}");
    let answer = server.request(
        3,
        "execute",
        Some("SELECT id, content FROM docs ORDER BY id;"),
        "exact",
    );
    assert!(answer.contains(r#""id":3"#), "{answer}");
    assert!(answer.contains(r#""columns":["id","content"]"#), "{answer}");
    assert!(answer.contains(r#""rows":[[1,"你好"]]"#), "{answer}");
}

#[test]
fn an_empty_result_set_keeps_its_columns() {
    let dir = TempDir::new("empty");
    let (mut server, _handshake) = Server::start(&dir.0);
    server.request(
        1,
        "execute",
        Some("CREATE TABLE docs (id BIGINT PRIMARY KEY);"),
        "exact",
    );
    let answer = server.request(2, "execute", Some("SELECT id FROM docs;"), "exact");
    assert!(answer.contains(r#""columns":["id"]"#), "{answer}");
    assert!(answer.contains(r#""rows":[]"#), "{answer}");
}

#[test]
fn a_failing_statement_does_not_end_the_session() {
    let dir = TempDir::new("failure");
    let (mut server, _handshake) = Server::start(&dir.0);
    server.request(
        1,
        "execute",
        Some("CREATE TABLE docs (id BIGINT PRIMARY KEY);"),
        "exact",
    );
    server.request(2, "execute", Some("INSERT INTO docs VALUES (1);"), "exact");
    // A duplicate primary key fails, and the session keeps going.
    let answer = server.request(3, "execute", Some("INSERT INTO docs VALUES (1);"), "exact");
    assert!(answer.contains(r#""ok":false"#), "{answer}");
    assert!(answer.contains(r#""code":"duplicate_key""#), "{answer}");
    assert!(answer.contains(r#""id":3"#), "{answer}");
    // The session still works.
    let answer = server.request(4, "execute", Some("SELECT id FROM docs;"), "exact");
    assert!(answer.contains(r#""rows":[[1]]"#), "{answer}");
}

#[test]
fn the_request_id_is_echoed_with_its_type() {
    let dir = TempDir::new("id");
    let (mut server, _handshake) = Server::start(&dir.0);
    let answer = server.request(7, "execute", Some("CREATE TABLE t (id BIGINT PRIMARY KEY);"), "exact");
    assert!(answer.contains(r#""id":7"#), "{answer}");
    let answer = server.request(8, "execute", Some("SELECT id FROM t;"), "exact");
    assert!(answer.contains(r#""id":8"#), "{answer}");
}

#[test]
fn a_malformed_request_is_reported_and_the_session_continues() {
    let dir = TempDir::new("malformed");
    let (mut server, _handshake) = Server::start(&dir.0);
    let answer = server.raw("this is not JSON");
    assert!(answer.contains(r#""ok":false"#), "{answer}");
    assert!(answer.contains(r#""code":"bad_request""#), "{answer}");
    // The session still works.
    let answer = server.request(1, "execute", Some("CREATE TABLE t (id BIGINT PRIMARY KEY);"), "exact");
    assert!(answer.contains(r#""ok":true"#), "{answer}");
}

#[test]
fn an_unsupported_operation_is_reported_and_the_session_continues() {
    let dir = TempDir::new("op");
    let (mut server, _handshake) = Server::start(&dir.0);
    let answer = server.request(1, "frobnicate", None, "exact");
    assert!(answer.contains(r#""ok":false"#), "{answer}");
    let answer = server.request(2, "execute", Some("CREATE TABLE t (id BIGINT PRIMARY KEY);"), "exact");
    assert!(answer.contains(r#""ok":true"#), "{answer}");
}

#[test]
fn the_shutdown_answers_and_ends_the_process() {
    let dir = TempDir::new("shutdown");
    let (mut server, _handshake) = Server::start(&dir.0);
    server.request(1, "execute", Some("CREATE TABLE t (id BIGINT PRIMARY KEY);"), "exact");
    let answer = server.request(2, "shutdown", None, "exact");
    assert!(answer.contains(r#""id":2"#), "{answer}");
    assert!(answer.contains(r#""ok":true"#), "{answer}");
    // The process ends by itself, with the code 0.
    let status = server.child.wait().expect("the process ends");
    assert_eq!(status.code(), Some(0));
    // Nothing is written after the shutdown answer.
    let mut extra = String::new();
    let n = server.stdout.read_line(&mut extra).expect("the output is readable");
    assert_eq!(n, 0, "the database wrote something after the shutdown: {extra:?}");
}

#[test]
fn a_restart_keeps_the_committed_rows() {
    let dir = TempDir::new("restart");
    {
        let (mut server, _handshake) = Server::start(&dir.0);
        server.request(
            1,
            "execute",
            Some("CREATE TABLE docs (id BIGINT PRIMARY KEY, content VARCHAR(16));"),
            "exact",
        );
        server.request(2, "execute", Some("INSERT INTO docs VALUES (1, '初始');"), "exact");
        server.request(3, "shutdown", None, "exact");
        let _ = server.child.wait();
    }
    // The second session reads the same data directory.
    let (mut server, _handshake) = Server::start(&dir.0);
    let answer = server.request(1, "execute", Some("SELECT id, content FROM docs;"), "exact");
    assert!(answer.contains(r#""rows":[[1,"初始"]]"#), "{answer}");
}

#[test]
fn an_open_transaction_is_rolled_back_at_the_end_of_the_session() {
    let dir = TempDir::new("rollback");
    {
        let (mut server, _handshake) = Server::start(&dir.0);
        server.request(
            1,
            "execute",
            Some("CREATE TABLE docs (id BIGINT PRIMARY KEY);"),
            "exact",
        );
        server.request(2, "execute", Some("INSERT INTO docs VALUES (1);"), "exact");
        server.request(3, "execute", Some("BEGIN;"), "exact");
        server.request(4, "execute", Some("INSERT INTO docs VALUES (2);"), "exact");
        // The judge closes the connection without a shutdown request.
        drop(server);
    }
    let (mut server, _handshake) = Server::start(&dir.0);
    let answer = server.request(1, "execute", Some("SELECT id FROM docs;"), "exact");
    assert!(answer.contains(r#""rows":[[1]]"#), "{answer}");
}

#[test]
fn the_search_mode_follows_the_request() {
    let dir = TempDir::new("mode");
    let (mut server, _handshake) = Server::start(&dir.0);
    server.request(
        1,
        "execute",
        Some("CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(2));"),
        "exact",
    );
    server.request(2, "execute", Some("INSERT INTO t VALUES (1, '[1,0]');"), "exact");
    server.request(
        3,
        "execute",
        Some("CREATE VECTOR INDEX idx_t ON t(v);"),
        "exact",
    );
    // The exact mode and the approximate mode give the same answer.
    let exact = server.request(
        4,
        "execute",
        Some("SELECT id FROM t ORDER BY L2_DISTANCE(v, '[0,0]') LIMIT 1;"),
        "exact",
    );
    let ann = server.request(
        5,
        "execute",
        Some("SELECT id FROM t ORDER BY L2_DISTANCE(v, '[0,0]') LIMIT 1;"),
        "ann",
    );
    let want = r#""rows":[[1]]"#;
    assert!(exact.contains(want), "{exact}");
    assert!(ann.contains(want), "{ann}");
}

#[test]
fn a_big_integer_keeps_its_precision() {
    let dir = TempDir::new("bigint");
    let (mut server, _handshake) = Server::start(&dir.0);
    server.request(
        1,
        "execute",
        Some("CREATE TABLE t (id BIGINT PRIMARY KEY, v BIGINT);"),
        "exact",
    );
    server.request(
        2,
        "execute",
        Some("INSERT INTO t VALUES (9007199254740993, -9007199254740993);"),
        "exact",
    );
    let answer = server.request(3, "execute", Some("SELECT id, v FROM t;"), "exact");
    assert!(
        answer.contains(r#""rows":[[9007199254740993,-9007199254740993]]"#),
        "{answer}"
    );
}

/// A truncated data file must not take the database down.
///
/// The admission gate reads a process that died before it answered as a
/// failure of the whole submission, so the recovery of a data directory that
/// was interrupted in the middle of a write has to end in a database that
/// starts and answers - not in a panic.
#[test]
fn a_truncated_column_file_does_not_take_the_database_down() {
    let dir = TempDir::new("torn-col");
    {
        let (mut server, _handshake) = Server::start(&dir.0);
        server.request(
            1,
            "execute",
            Some("CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(4));"),
            "exact",
        );
        for i in 0..20 {
            server.request(
                2,
                "execute",
                Some(&format!(
                    "INSERT INTO t VALUES ({i}, 'c{i}', '[{i},0,0,0]');"
                )),
                "exact",
            );
        }
        server.request(3, "shutdown", None, "exact");
        let _ = server.child.wait();
    }
    // The column file is cut short, as a write that was interrupted leaves it.
    let col = dir.0.join("t").join("col.dat");
    let len = std::fs::metadata(&col).unwrap().len();
    assert!(len > 40, "the test needs a file to cut");
    let handle = std::fs::OpenOptions::new().write(true).open(&col).unwrap();
    handle.set_len(len - 30).unwrap();
    drop(handle);

    // The database still starts and still answers.
    let (mut server, handshake) = Server::start(&dir.0);
    assert_eq!(handshake, r#"{"type":"ready"}"#);
    let answer = server.request(1, "execute", Some("SELECT id FROM t LIMIT 3;"), "exact");
    assert!(answer.contains(r#""ok":true"#), "{answer}");
}

/// A catalog file that was damaged must not take the database down.
///
/// The catalog holds the schemas once the log has been folded into the data
/// files, so a damaged one is recovered from the backup. Either way the
/// process has to stay alive and answer.
#[test]
fn a_damaged_catalog_does_not_take_the_database_down() {
    let dir = TempDir::new("torn-catalog");
    {
        let (mut server, _handshake) = Server::start(&dir.0);
        server.request(
            1,
            "execute",
            Some("CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(4));"),
            "exact",
        );
        server.request(
            2,
            "execute",
            Some("INSERT INTO t VALUES (1, 'c1', '[1,0,0,0]');"),
            "exact",
        );
        server.request(3, "shutdown", None, "exact");
        let _ = server.child.wait();
    }
    // The magic of the catalog is overwritten.
    let catalog = dir.0.join("catalog.dat");
    let mut handle = std::fs::OpenOptions::new().write(true).open(&catalog).unwrap();
    use std::io::Write;
    handle.write_all(b"XXXXXXXX").unwrap();
    drop(handle);

    let (mut server, handshake) = Server::start(&dir.0);
    assert_eq!(handshake, r#"{"type":"ready"}"#);
    // The backup holds the schema, so the table is still there.
    let answer = server.request(1, "execute", Some("SELECT id, cat FROM t;"), "exact");
    assert!(answer.contains(r#""rows":[[1,"c1"]]"#), "{answer}");
}

/// A directory entry that names an offset past the end of the file must not
/// take the database down.
///
/// The entry is read as a row that cannot be decoded, rather than as an
/// allocation of the size it asks for: a huge allocation would be answered by
/// the out of memory killer, and the process would die before the handshake.
#[test]
fn a_directory_entry_with_a_huge_offset_does_not_take_the_database_down() {
    let dir = TempDir::new("huge-offset");
    {
        let (mut server, _handshake) = Server::start(&dir.0);
        server.request(
            1,
            "execute",
            Some("CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(4));"),
            "exact",
        );
        for i in 0..8 {
            server.request(
                2,
                "execute",
                Some(&format!("INSERT INTO t VALUES ({i}, '[{i},0,0,0]');")),
                "exact",
            );
        }
        server.request(3, "shutdown", None, "exact");
        let _ = server.child.wait();
    }
    // The offset of the first row is replaced by a value that is far past the
    // end of the column file.
    let meta = dir.0.join("t").join("meta.dat");
    let mut buf = std::fs::read(&meta).unwrap();
    buf[8..16].copy_from_slice(&u64::MAX.to_le_bytes());
    std::fs::write(&meta, &buf).unwrap();

    let (mut server, handshake) = Server::start(&dir.0);
    assert_eq!(handshake, r#"{"type":"ready"}"#);
    let answer = server.request(1, "execute", Some("SELECT id FROM t LIMIT 3;"), "exact");
    assert!(answer.contains(r#""ok":true"#), "{answer}");
}
