//! Statement classification is a first guard. Drivers additionally enforce read-only transactions.
use crate::*;
use regex::Regex;

/// Blanks comments and quoted contents while preserving statement punctuation and byte length.
/// Unterminated quotes/comments fail closed rather than accidentally treating their suffix as SQL.
pub fn skeleton(sql: &str) -> Result<String> {
    skeleton_for(sql, Engine::Postgres)
}
fn skeleton_for(sql: &str, engine: Engine) -> Result<String> {
    let b = sql.as_bytes();
    let mut out = vec![b' '; b.len()];
    let mut i = 0;
    while i < b.len() {
        if b[i..].starts_with(b"--") {
            while i < b.len() && b[i] != b'\n' {
                i += 1;
            }
            continue;
        }
        if b[i..].starts_with(b"/*") {
            let mut depth = 1;
            i += 2;
            while i < b.len() && depth > 0 {
                if b[i..].starts_with(b"/*") {
                    depth += 1;
                    i += 2;
                } else if b[i..].starts_with(b"*/") {
                    depth -= 1;
                    i += 2;
                } else {
                    i += 1;
                }
            }
            if depth != 0 {
                return Err(Error::invalid("Unterminated SQL comment"));
            }
            continue;
        }
        if matches!(b[i], b'\'' | b'"' | b'`') {
            let quote = b[i];
            let escape = matches!(engine, Engine::MySql | Engine::Neptune)
                || (quote == b'\''
                    && i > 0
                    && matches!(b[i - 1], b'e' | b'E')
                    && (i < 2 || !b[i - 2].is_ascii_alphanumeric()));
            i += 1;
            let mut done = false;
            while i < b.len() {
                if escape && b[i] == b'\\' {
                    i = (i + 2).min(b.len());
                } else if b[i] == quote {
                    i += 1;
                    if i < b.len() && b[i] == quote {
                        i += 1;
                    } else {
                        done = true;
                        break;
                    }
                } else {
                    i += 1;
                }
            }
            if !done {
                return Err(Error::invalid("Unterminated SQL quote"));
            }
            continue;
        }
        if b[i] == b'$' {
            let mut end = i + 1;
            while end < b.len() && (b[end].is_ascii_alphanumeric() || b[end] == b'_') {
                end += 1;
            }
            if end < b.len() && b[end] == b'$' {
                let marker = &sql[i..=end];
                let start = end + 1;
                let Some(tail) = sql[start..].find(marker) else {
                    return Err(Error::invalid("Unterminated SQL dollar quote"));
                };
                i = start + tail + marker.len();
                continue;
            }
        }
        if b[i].is_ascii() {
            out[i] = b[i];
        }
        i += 1;
    }
    String::from_utf8(out).map_err(|_| Error::invalid("Invalid SQL encoding"))
}
fn matches(pattern: &str, s: &str) -> bool {
    Regex::new(pattern).expect("constant regex").is_match(s)
}
pub fn is_read_only(engine: Engine, query: &str) -> Result<bool> {
    if engine == Engine::Redis {
        return redis_read_only(query);
    }
    let sk = skeleton_for(query, engine)?;
    let statements: Vec<_> = sk.split(';').filter(|s| !s.trim().is_empty()).collect();
    if statements.len() != 1 {
        return Ok(false);
    }
    let sk = statements[0].trim_start();
    if engine == Engine::Neptune {
        return Ok(matches(
            r"(?i)^(match|optional|return|with|unwind|explain|profile)\b",
            sk,
        ) && !matches(
            r"(?i)\b(create|merge|set|delete|detach|remove|drop|call|foreach|load)\b",
            &Regex::new(r"[.:]\s*\w+")
                .unwrap()
                .replace_all(&top_level(sk), " "),
        ));
    }
    if engine == Engine::Sqlite && matches(r"(?i)^pragma\b", sk) {
        return Ok(!sk.contains('=')
            && matches(
                r"(?i)^pragma\s+(?:[a-z_]+\.)?(table_info|table_xinfo|index_list|index_info|index_xinfo|foreign_key_list|database_list|compile_options|foreign_keys|query_only|schema_version|user_version|encoding|journal_mode|page_count|page_size|freelist_count)\b",
                sk,
            ));
    }
    if !matches(r"(?i)^(select|with|show|explain|table|values)\b", sk) {
        return Ok(false);
    }
    if matches(r"(?i)\binto\b", sk) {
        return Ok(false);
    }
    if matches(r"(?i)^with\b", sk) && matches(r"(?i)\b(insert|update|delete|merge)\b", sk) {
        return Ok(false);
    }
    if matches(r"(?i)^explain\b", sk)
        && matches(
            r"(?i)\b(insert|update|delete|merge|create|drop|alter|call)\b",
            sk,
        )
    {
        return Ok(false);
    }
    Ok(true)
}
pub fn redis_read_only(command: &str) -> Result<bool> {
    let args =
        shell_words::split(command).map_err(|_| Error::invalid("Invalid Redis command quoting"))?;
    let Some(head) = args.first() else {
        return Ok(false);
    };
    let head = head.to_ascii_uppercase();
    let allowed="GET MGET GETRANGE STRLEN EXISTS TYPE TTL PTTL EXPIRETIME PEXPIRETIME HGET HMGET HGETALL HEXISTS HKEYS HVALS HLEN HSTRLEN HSCAN HTTL HPTTL HEXPIRETIME HPEXPIRETIME HRANDFIELD LINDEX LLEN LRANGE LPOS SCARD SISMEMBER SMISMEMBER SMEMBERS SRANDMEMBER SSCAN SINTER SINTERCARD SUNION SDIFF ZCARD ZCOUNT ZLEXCOUNT ZRANGE ZRANGEBYSCORE ZRANGEBYLEX ZREVRANGE ZREVRANGEBYSCORE ZREVRANGEBYLEX ZRANK ZREVRANK ZSCORE ZMSCORE ZSCAN ZRANDMEMBER ZDIFF ZINTER ZINTERCARD ZUNION GETBIT BITCOUNT BITPOS BITFIELD BITFIELD_RO PFCOUNT GEODIST GEOHASH GEOPOS GEORADIUS GEORADIUSBYMEMBER GEOSEARCH XLEN XRANGE XREVRANGE XREAD XINFO SCAN KEYS RANDOMKEY DBSIZE PING ECHO TIME INFO ROLE COMMAND OBJECT MEMORY PUBSUB DUMP SORT SORT_RO --SCAN";
    if !allowed.split_whitespace().any(|v| v == head) {
        return Ok(false);
    }
    let forbidden: &[&str] = match head.as_str() {
        "SORT" => &["STORE"],
        "GEORADIUS" | "GEORADIUSBYMEMBER" => &["STORE", "STOREDIST"],
        "BITFIELD" => &["SET", "INCRBY", "OVERFLOW"],
        "MEMORY" => &["PURGE"],
        _ => &[],
    };
    if args
        .iter()
        .skip(1)
        .any(|arg| forbidden.iter().any(|v| arg.eq_ignore_ascii_case(v)))
    {
        return Ok(false);
    }

    Ok(true)
}
/// Parameter interpolation understands strings/comments so substituted text cannot be re-substituted.
/// `:'name'` is a quoted literal; `:name` remains an explicit raw substitution in trusted saved SQL.
pub fn substitute(
    engine: Engine,
    query: &str,
    params: &BTreeMap<String, String>,
) -> Result<String> {
    let b = query.as_bytes();
    let mut out = String::new();
    let mut i = 0;
    while i < b.len() {
        if b[i..].starts_with(b"--") {
            let end = query[i..].find('\n').map(|n| i + n).unwrap_or(b.len());
            out.push_str(&query[i..end]);
            i = end;
            continue;
        }
        if b[i..].starts_with(b"/*") {
            let start = i;
            let mut depth = 1;
            i += 2;
            while i < b.len() && depth > 0 {
                if b[i..].starts_with(b"/*") {
                    depth += 1;
                    i += 2
                } else if b[i..].starts_with(b"*/") {
                    depth -= 1;
                    i += 2
                } else {
                    i += 1
                }
            }
            if depth != 0 {
                return Err(Error::invalid("Unterminated SQL comment"));
            }
            out.push_str(&query[start..i]);
            continue;
        }
        if b[i] == b':' && (i == 0 || b[i - 1] != b':') && i + 1 < b.len() {
            let quoted = b[i + 1] == b'\'';
            let start = i + if quoted { 2 } else { 1 };
            let mut end = start;
            while end < b.len() && (b[end].is_ascii_alphanumeric() || b[end] == b'_') {
                end += 1;
            }
            if end > start && (!quoted || (end < b.len() && b[end] == b'\'')) {
                let name = &query[start..end];
                if let Some(value) = params.get(name) {
                    if quoted {
                        let escaped = value.replace('\'', "''");
                        let escaped = if matches!(engine, Engine::MySql | Engine::Postgres) {
                            escaped.replace('\\', "\\\\")
                        } else {
                            escaped
                        };
                        if engine == Engine::Postgres {
                            out.push('E');
                        }
                        out.push('\'');
                        out.push_str(&escaped);
                        out.push('\'');
                    } else {
                        out.push_str(value);
                    }
                    i = end + usize::from(quoted);
                    continue;
                }
            }
        }
        if matches!(b[i], b'\'' | b'"' | b'`') {
            let start = i;
            let quote = b[i];
            let escape = matches!(engine, Engine::MySql | Engine::Neptune)
                || (quote == b'\''
                    && i > 0
                    && matches!(b[i - 1], b'e' | b'E')
                    && (i < 2 || !b[i - 2].is_ascii_alphanumeric()));
            i += 1;
            let mut done = false;
            while i < b.len() {
                if escape && b[i] == b'\\' {
                    i = (i + 2).min(b.len());
                } else if b[i] == quote {
                    i += 1;
                    if i < b.len() && b[i] == quote {
                        i += 1
                    } else {
                        done = true;
                        break;
                    }
                } else {
                    i += 1
                }
            }
            if !done {
                return Err(Error::invalid("Unterminated SQL quote"));
            }
            out.push_str(&query[start..i]);
            continue;
        }
        if b[i] == b'$' {
            let mut end = i + 1;
            while end < b.len() && (b[end].is_ascii_alphanumeric() || b[end] == b'_') {
                end += 1;
            }
            if end < b.len() && b[end] == b'$' {
                let marker = &query[i..=end];
                let start = end + 1;
                let Some(n) = query[start..].find(marker) else {
                    return Err(Error::invalid("Unterminated dollar quote"));
                };
                let last = start + n + marker.len();
                out.push_str(&query[i..last]);
                i = last;
                continue;
            }
        }
        let ch = query[i..].chars().next().unwrap();
        out.push(ch);
        i += ch.len_utf8();
    }
    Ok(out)
}
fn top_level(sk: &str) -> String {
    // Keep byte positions stable: callers use these offsets to edit original SQL,
    // including queries with non-ASCII identifiers inside nested subqueries.
    let mut depth = 0usize;
    let mut output = sk.as_bytes().to_vec();
    for (index, &byte) in sk.as_bytes().iter().enumerate() {
        match byte {
            b'(' | b'[' | b'{' => {
                depth += 1;
                output[index] = b' ';
            }
            b')' | b']' | b'}' => {
                depth = depth.saturating_sub(1);
                output[index] = b' ';
            }
            _ if depth > 0 => output[index] = b' ',
            _ => {}
        }
    }
    String::from_utf8(output).expect("Replacing nested UTF-8 bytes with spaces preserves UTF-8")
}

pub fn prepare(engine: Engine, request: &QueryRequest) -> Result<(String, Option<usize>)> {
    if request.query.trim().is_empty() {
        return Err(Error::invalid("Query is empty"));
    }
    if request.policy.max_bytes == 0 {
        return Err(Error::invalid("Response budget must be positive"));
    }
    let sql = substitute(engine, &request.query, &request.parameters)?;
    if engine == Engine::Redis {
        if !request.policy.allow_write && !redis_read_only(&sql)? {
            return Err(Error::safety(
                "Redis write command requires explicit authorization",
            ));
        }
        return Ok((sql, request.policy.max_rows.filter(|n| *n > 0)));
    }
    let sk = skeleton_for(&sql, engine)?;
    if sk.contains('\\') || sk.split(';').filter(|s| !s.trim().is_empty()).count() != 1 {
        return Err(Error::safety(
            "Execute one SQL statement; client commands are not supported",
        ));
    }
    if engine == Engine::MySql && sql.contains("/*!") {
        return Err(Error::safety("Executable MySQL comments are not supported"));
    }
    if engine == Engine::Sqlite
        && (matches(
            r"(?i)^\s*(attach|detach|begin|commit|end|rollback|savepoint|release|vacuum)\b",
            &sk,
        ) || matches(r"(?i)\b(load_extension|writefile|readfile)\s*\(", &sk))
    {
        return Err(Error::safety(
            "SQLite transaction, external file and extension commands are not supported",
        ));
    }
    let readonly = is_read_only(engine, &sql)?;
    if !request.policy.allow_write && !readonly {
        return Err(Error::safety(
            "Read-only query: writes/DDL require explicit authorization",
        ));
    }
    let cap = request.policy.max_rows.filter(|n| *n > 0);
    let top = top_level(&sk);
    if let Some(cap) =
        cap.filter(|_| readonly && !matches(r"(?i)\b(limit|fetch\s+(first|next))\b", &top))
    {
        let trimmed = sql.trim().trim_end_matches(';');
        if engine == Engine::Neptune && matches(r"(?i)\breturn\b", &top) {
            return Ok((
                format!(
                    "{trimmed}\nSKIP {} LIMIT {}",
                    request.policy.offset,
                    cap.saturating_add(1)
                ),
                Some(cap),
            ));
        }
        if matches!(engine, Engine::Postgres | Engine::MySql | Engine::Sqlite)
            && matches(r"(?i)^\s*(select|with|table|values)\b", &top)
            && !matches(
                r"(?i)\bfor\s+(update|share|no\s+key\s+update|key\s+share)\b",
                &top,
            )
        {
            if engine == Engine::Sqlite && matches(r"(?i)^\s*values\b", &top) {
                return Ok((sql, Some(cap)));
            }
            return Ok((
                format!(
                    "{trimmed}\nLIMIT {} OFFSET {}",
                    cap.saturating_add(1),
                    request.policy.offset
                ),
                Some(cap),
            ));
        }
        if engine == Engine::Sqlite {
            return Ok((sql, Some(cap)));
        }
    }
    Ok((sql, None))
}
/// Explicit CLI full/limit operations may replace the outer result limit.
/// Normal execution never rewrites an explicit user limit.
pub fn strip_outer_limit(sql: &str, engine: Engine) -> Result<String> {
    let Some((start, end)) = outer_limit_bounds(sql, engine)? else {
        return Ok(sql.to_owned());
    };
    let suffix = &sql[end..];
    let separator = if suffix.is_empty()
        || suffix.starts_with(';')
        || suffix.starts_with(char::is_whitespace)
    {
        ""
    } else {
        " "
    };
    Ok(
        format!("{}{}{}", sql[..start].trim_end(), separator, suffix)
            .trim_end()
            .to_owned(),
    )
}
pub fn override_outer_limit(sql: &str, limit: usize, engine: Engine) -> Result<String> {
    if let Some((start, end)) = outer_limit_bounds(sql, engine)? {
        let suffix = &sql[end..];
        return Ok(format!(
            "{}LIMIT {}{}{}",
            &sql[..start],
            limit,
            if suffix.starts_with(char::is_whitespace)
                || suffix.starts_with(';')
                || suffix.is_empty()
            {
                ""
            } else {
                " "
            },
            suffix
        ));
    }
    let skeleton = skeleton_for(sql, engine)?;
    let top = top_level(&skeleton);
    if engine == Engine::Sqlite && matches(r"(?i)^\s*values\b", &top) {
        return Ok(format!(
            "SELECT * FROM (\n{}\n) LIMIT {limit}",
            sql.trim().trim_end_matches(';')
        ));
    }
    let mut query = sql.to_owned();
    if let Some(index) = skeleton.rfind(';') {
        if skeleton[index + 1..].trim().is_empty() {
            query.remove(index);
        }
    }
    let top = top_level(&skeleton);
    if let Some(lock) = Regex::new(r"(?i)\bFOR\s+(?:UPDATE|SHARE|NO\s+KEY\s+UPDATE|KEY\s+SHARE)\b")
        .unwrap()
        .find(&top)
    {
        return Ok(format!(
            "{} LIMIT {limit} {}",
            query[..lock.start()].trim_end(),
            query[lock.start()..].trim_start()
        ));
    }
    Ok(format!("{query}\nLIMIT {limit}"))
}
fn outer_limit_bounds(sql: &str, engine: Engine) -> Result<Option<(usize, usize)>> {
    if engine == Engine::Redis {
        return Ok(None);
    }
    let skeleton = skeleton_for(sql, engine)?;
    let top = top_level(&skeleton);
    let pattern = Regex::new(r"(?i)\b(?:LIMIT|FETCH\s+(?:FIRST|NEXT)|OFFSET|SKIP)\b").unwrap();
    let candidates = pattern
        .find_iter(&top)
        .filter(|m| {
            m.start() == 0 || !matches!(top.as_bytes()[m.start() - 1], b'.' | b':' | b'@' | b'$')
        })
        .collect::<Vec<_>>();
    let Some(first) = candidates.first() else {
        return Ok(None);
    };
    let start = first.start();
    let tail = &top[start..];
    let end = tail.find(';').map(|n| start + n).unwrap_or(sql.len());
    let locking =
        Regex::new(r"(?i)\bFOR\s+(?:UPDATE|SHARE|NO\s+KEY\s+UPDATE|KEY\s+SHARE)\b").unwrap();
    let end = locking
        .find(&top[start..end])
        .map(|m| start + m.start())
        .unwrap_or(end);
    Ok(Some((start, end)))
}
#[cfg(test)]
mod limit_override_tests {
    use super::*;
    #[test]
    fn preserves_inner_limits_quoted_identifiers_and_trailing_locks() {
        let query = "SELECT * FROM (SELECT * FROM t LIMIT 2) x LIMIT 5 OFFSET 3 FOR UPDATE";
        assert_eq!(
            strip_outer_limit(query, Engine::Postgres).unwrap(),
            "SELECT * FROM (SELECT * FROM t LIMIT 2) x FOR UPDATE"
        );
        assert!(override_outer_limit(query, 9, Engine::Postgres)
            .unwrap()
            .ends_with("LIMIT 9 FOR UPDATE"));
        assert_eq!(
            strip_outer_limit("SELECT \"limit\" FROM t", Engine::Postgres).unwrap(),
            "SELECT \"limit\" FROM t"
        );
    }
    #[test]
    fn unicode_nested_identifiers_keep_original_byte_positions() {
        let sql = "SELECT * FROM (SELECT 中文 FROM 数据 LIMIT 2) nested LIMIT 7";
        assert_eq!(
            strip_outer_limit(sql, Engine::Postgres).unwrap(),
            "SELECT * FROM (SELECT 中文 FROM 数据 LIMIT 2) nested"
        );
        assert!(override_outer_limit(sql, 4, Engine::Postgres)
            .unwrap()
            .ends_with("nested LIMIT 4"));
    }
    #[test]
    fn sqlite_expressions_comments_and_neptune_skip() {
        assert_eq!(
            strip_outer_limit(
                "SELECT * FROM t LIMIT (2+3) OFFSET 1; -- tail",
                Engine::Sqlite
            )
            .unwrap(),
            "SELECT * FROM t; -- tail"
        );
        assert_eq!(
            strip_outer_limit("MATCH(n) RETURN n SKIP 3 LIMIT 4", Engine::Neptune).unwrap(),
            "MATCH(n) RETURN n"
        );
        assert!(override_outer_limit("VALUES(1),(2)", 1, Engine::Sqlite)
            .unwrap()
            .starts_with("SELECT * FROM ("));
        assert!(
            override_outer_limit("SELECT ';' --comment", 2, Engine::Postgres)
                .unwrap()
                .ends_with("\nLIMIT 2")
        );
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn comments_literals_batches() {
        assert!(is_read_only(Engine::Postgres, "SELECT '; delete' /* update */").unwrap());
        assert!(!is_read_only(Engine::Postgres, "SELECT 1; DELETE FROM x").unwrap());
        assert!(!is_read_only(
            Engine::Postgres,
            "WITH x AS (DELETE FROM y RETURNING *) SELECT * FROM x"
        )
        .unwrap());
        assert!(skeleton("select $tag$;drop$tag$")
            .unwrap()
            .starts_with("select"));
        assert!(skeleton("select 'broken").is_err());
    }
    #[test]
    fn limits() {
        let r = QueryRequest::new("select * from (select * from x limit 1) y");
        assert!(prepare(Engine::Postgres, &r)
            .unwrap()
            .0
            .ends_with("LIMIT 501 OFFSET 0"));
        let r = QueryRequest::new("select * from x fetch first 5 rows only");
        assert_eq!(prepare(Engine::Postgres, &r).unwrap().1, None);
        let r = QueryRequest::new("select * from x for update");
        assert_eq!(prepare(Engine::Postgres, &r).unwrap().1, None);
    }
    #[test]
    fn params_are_not_resubstituted() {
        let mut p = BTreeMap::new();
        p.insert("x".into(), ":y'; DROP TABLE x; --".into());
        p.insert("y".into(), "unsafe".into());
        let s = substitute(Engine::Sqlite, "select :'x', ':y' -- :y", &p).unwrap();
        assert!(s.contains(":y''; DROP"));
        assert!(is_read_only(Engine::Sqlite, &s).unwrap());
        assert!(s.ends_with("-- :y"));
    }
    #[test]
    fn redis_conditional_mutations() {
        for s in [
            "GETDEL a",
            "SORT a STORE b",
            "EVAL 'return 1' 0",
            "XREADGROUP GROUP a b STREAMS x >",
            "ZUNIONSTORE dest 2 a b",
        ] {
            assert!(!redis_read_only(s).unwrap(), "{s}");
        }
        assert!(redis_read_only("GET 'newline\\nkey'").unwrap());
    }
}
