"""
SQL Checker for DMOJ

Executes both the expected SQL and the student's SQL against a SQLite database,
then compares the results.

Usage in init.yml:
    checker:
        name: sql_checker.py
        args:
            db_file: miniwind.db          # filename of the DB inside the problem directory
            order_matters: false           # whether row order matters (default: false, SELECT only)
            compare_columns: false         # whether column names must match (default: false, SELECT only)
            float_tolerance: 0.001         # tolerance for float comparison (default: 0.001)
            question_index: 2              # optional: which multi-question answer to grade (see below)

The student submits raw SQL as their "source code" (language: TEXT).

Statement type (SELECT / INSERT / UPDATE -- NOT DELETE, deliberately unsupported for now) is
detected automatically from the reference query stored in the case's .out file -- no extra
configuration needed. A SELECT case is graded exactly as before (read-only, compare result sets).
An INSERT/UPDATE case is graded by applying the statement to a **throwaway copy** of the database
and comparing the resulting content of every table against a second copy where the *reference*
statement was applied -- the original .db file on disk is never opened for writing, not even
transiently; see _make_temp_copy(). Because the whole database is compared, a reference INSERT/
UPDATE should specify explicit values for any primary key it touches rather than relying on
auto-increment, or the comparison could spuriously fail.

Multi-question problems: when a problem asks several questions about the same database, the
web app's submission form joins every answer box into a single source blob, each one preceded
by a marker line of the form "-- @@Q<n>@@" (n being 1-based, in the order the boxes were shown).
If `question_index` is configured for a given test case, only the segment following the matching
marker is graded by that case; if no marker is found at all (older, single-question problems),
the whole submission is graded as-is, exactly like before this feature existed.
"""
import os
import re
import shutil
import sqlite3
import tempfile
import time
import traceback
from contextlib import contextmanager

from dmoj.judgeenv import get_problem_root
from dmoj.result import CheckerResult

_QUESTION_MARKER_RE = re.compile(r'^[ \t]*--[ \t]*@@Q(\d+)@@[ \t]*\r?$', re.MULTILINE)

# Statement types a student may ever submit. DELETE is deliberately NOT included yet.
_ALLOWED_STATEMENT_TYPES = ('SELECT', 'INSERT', 'UPDATE')

# Always rejected, regardless of what the case allows. ATTACH/DETACH are the key ones: they are
# SQLite's mechanism for opening an *additional* arbitrary file as a database within the same
# connection, which is exactly the kind of "escape the assigned .db file" this list exists to
# prevent. USE has no equivalent in SQLite (it's a multi-schema-per-connection concept from
# engines like MySQL) but is listed explicitly anyway so the restriction is visible in the code,
# not just an accident of SQLite's dialect. REPLACE is DELETE+INSERT in one statement, so allowing
# it would silently reintroduce DELETE before it's meant to be supported.
_ALWAYS_FORBIDDEN = ['DELETE', 'DROP', 'ALTER', 'CREATE', 'TRUNCATE', 'EXEC', 'EXECUTE',
                     'ATTACH', 'DETACH', 'PRAGMA', 'VACUUM', 'REINDEX', 'USE', 'REPLACE']

# Prepended to the feedback of a _ALWAYS_FORBIDDEN rejection so judge/bridge/judge_handler.py's
# on_test_case can recognize this specific case as a deliberate destructive/escaping attempt (not
# an ordinary wrong answer) and mark it with the 'SEC' status instead of 'WA', email the admins,
# and show a dedicated warning on the submission page. Must match the constant of the same name in
# that file exactly. NOT applied to ordinary mistakes (wrong statement type for this question,
# multiple statements) -- only to the explicit destructive-keyword blocklist below.
SECURITY_VIOLATION_MARKER = '@@SECVIOL@@'

# Wall-clock budget for a single query run by student-controlled SQL (SELECT, or the write
# statement against its throwaway copy). Defense in depth against a pathological query (e.g. a
# huge self-join) hanging the judge process, independent of the submission's own time limit.
_QUERY_TIME_BUDGET_SECONDS = 5


def _extract_question_segment(full_text, question_index):
    markers = list(_QUESTION_MARKER_RE.finditer(full_text))
    if not markers:
        return None
    for i, marker in enumerate(markers):
        if int(marker.group(1)) != question_index:
            continue
        start = marker.end()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(full_text)
        return full_text[start:end].strip()
    return None


def check(process_output, judge_output,
          db_file='miniwind.db', order_matters=False, compare_columns=False,
          float_tolerance=0.001, point_value=1, problem_id=None, question_index=None, **kwargs):
    """
    DMOJ checker interface.

    process_output: stdout of the student's program (= the SQL query, via TEXT executor)
    judge_output:   content of the .out file (= the correct SQL query)
    problem_id:     passed by DMOJ in kwargs, used to locate the DB file
    question_index: passed by DMOJ if this problem asks more than one question (see module docstring)
    """

    student_sql_full = _to_str(process_output).strip()
    expected_sql = _to_str(judge_output).strip()

    if not student_sql_full:
        return CheckerResult(False, 0, feedback="Empty submission - no SQL query provided")

    if question_index is not None and _QUESTION_MARKER_RE.search(student_sql_full):
        # Only split by question if the submission actually contains markers -- a bare
        # submission with no markers at all (e.g. via the API, or an older client) is graded
        # as a single whole query instead of being rejected outright.
        segment = _extract_question_segment(student_sql_full, int(question_index))
        if segment is None:
            return CheckerResult(False, 0,
                                 feedback="No answer found for question %d" % int(question_index))
        student_sql = segment
    else:
        student_sql = student_sql_full

    if not student_sql:
        return CheckerResult(False, 0, feedback="Empty submission - no SQL query provided")

    if not expected_sql:
        return CheckerResult(False, 0, feedback="No expected SQL configured (check .out file)")

    # Resolve DB path from problem directory
    db_path = None
    if problem_id:
        try:
            problem_root = get_problem_root(problem_id)
            db_path = os.path.join(problem_root, str(db_file))
        except Exception:
            pass

    # Fallback: try db_file as absolute path
    if not db_path or not os.path.exists(db_path):
        db_path = str(db_file)

    if not os.path.exists(db_path):
        return CheckerResult(False, 0, feedback="Database file not found: %s" % db_file)

    # The reference query in the .out file determines what kind of statement this case grades --
    # no separate configuration needed. An unrecognized reference statement is a problem setup
    # error, not something the student did wrong.
    statement_type = _detect_statement_type(expected_sql)
    if statement_type is None:
        return CheckerResult(False, 0,
                             feedback="Problem configuration error: reference SQL is not a "
                                      "recognized SELECT/INSERT/UPDATE statement")

    security_msg = _security_check(student_sql, statement_type)
    if security_msg:
        return CheckerResult(False, 0, feedback=security_msg)

    if statement_type == 'SELECT':
        return _check_select(db_path, student_sql, expected_sql, order_matters=bool(order_matters),
                             compare_columns=bool(compare_columns),
                             float_tolerance=float(float_tolerance), point_value=float(point_value))
    else:
        return _check_write(db_path, student_sql, expected_sql, point_value=float(point_value))


def _check_select(db_path, student_sql, expected_sql, order_matters, compare_columns,
                  float_tolerance, point_value):
    # Execute expected SQL
    try:
        expected_result = _execute_sql(db_path, expected_sql)
    except Exception as e:
        return CheckerResult(False, 0,
                             feedback="Internal error with expected SQL: %s" % str(e),
                             extended_feedback=traceback.format_exc())

    # Execute student SQL -- always against the real (read-only) db_path: a bare SELECT can never
    # mutate anything, so there is nothing to isolate it from.
    try:
        student_result = _execute_sql(db_path, student_sql)
    except sqlite3.OperationalError as e:
        return CheckerResult(False, 0, feedback="SQL error: %s" % str(e))
    except Exception as e:
        return CheckerResult(False, 0, feedback="Error executing your SQL: %s" % str(e))

    return _compare_results(
        student_result, expected_result,
        order_matters=order_matters,
        compare_columns=compare_columns,
        float_tolerance=float_tolerance,
        point_value=point_value,
    )


def _check_write(db_path, student_sql, expected_sql, point_value):
    # Apply the student's statement to its own throwaway copy of the database -- db_path itself
    # (the problem's real .db file) is only ever opened read-only (inside _make_temp_copy, via a
    # plain filesystem copy, not even through sqlite3) and is never touched by this function.
    try:
        with _make_temp_copy(db_path) as student_copy:
            try:
                _execute_write(student_copy, student_sql)
            except sqlite3.OperationalError as e:
                return CheckerResult(False, 0, feedback="SQL error: %s" % str(e))
            except Exception as e:
                return CheckerResult(False, 0, feedback="Error executing your SQL: %s" % str(e))
            student_state = _dump_all_tables(student_copy)

        with _make_temp_copy(db_path) as reference_copy:
            _execute_write(reference_copy, expected_sql)
            expected_state = _dump_all_tables(reference_copy)
    except Exception as e:
        return CheckerResult(False, 0,
                             feedback="Internal error grading this submission: %s" % str(e),
                             extended_feedback=traceback.format_exc())

    if student_state == expected_state:
        return CheckerResult(True, point_value, feedback="")
    return CheckerResult(False, 0,
                         feedback="The database's final content does not match what was expected "
                                  "after running your statement.")


def _to_str(data):
    if isinstance(data, bytes):
        return data.decode('utf-8', errors='replace')
    return str(data) if data else ''


def _clean_sql(sql):
    sql_upper = sql.upper().strip()
    sql_clean = re.sub(r'--.*$', '', sql_upper, flags=re.MULTILINE)
    sql_clean = re.sub(r'/\*.*?\*/', '', sql_clean, flags=re.DOTALL).strip()
    return sql_clean


def _first_keyword(sql_clean):
    """Extracts the leading SQL keyword, e.g. 'VACUUM' out of 'VACUUM;' or 'VACUUM(...)' -- a
    plain .split()[0] would leave punctuation glued to the word (no whitespace before the ';')
    and let it slip past an exact-match blocklist."""
    match = re.match(r'^([A-Z_]+)', sql_clean)
    return match.group(1) if match else ''


def _detect_statement_type(sql):
    """Returns 'SELECT', 'INSERT' or 'UPDATE' based on the leading keyword, or None if the
    statement isn't one of those (used on the .out reference query to decide the grading mode)."""
    first_word = _first_keyword(_clean_sql(sql))
    return first_word if first_word in _ALLOWED_STATEMENT_TYPES else None


def _security_check(sql, allowed_type):
    sql_clean = _clean_sql(sql)
    first_word = _first_keyword(sql_clean)

    if first_word in _ALWAYS_FORBIDDEN:
        return SECURITY_VIOLATION_MARKER + ("This statement type is not allowed: %s" % first_word)
    if first_word != allowed_type:
        return "Expected a %s statement for this question, found: %s" % (allowed_type, first_word or '(empty)')

    statements = [s.strip() for s in sql.split(';') if s.strip()]
    if len(statements) > 1:
        return "Only a single SQL statement is allowed"

    return None


def _install_time_budget(conn):
    deadline = time.time() + _QUERY_TIME_BUDGET_SECONDS
    conn.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 1000)


def _execute_sql(db_path, sql):
    conn = sqlite3.connect('file:%s?mode=ro' % db_path, uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        _install_time_budget(conn)
        cursor = conn.execute(sql)
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        rows = cursor.fetchall()
        return columns, rows
    finally:
        conn.close()


@contextmanager
def _make_temp_copy(db_path):
    """Copies db_path (a plain filesystem read+write of a NEW file -- db_path is never opened by
    sqlite3 here, so it can never be written to) to a fresh temporary file, yields its path, and
    always deletes it afterwards."""
    fd, temp_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    try:
        shutil.copyfile(db_path, temp_path)
        yield temp_path
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def _execute_write(db_path, sql):
    """Runs a single write statement against db_path, which must ALWAYS be a throwaway copy
    (see _make_temp_copy) -- this function opens its argument read-write with no further checks."""
    conn = sqlite3.connect(db_path)
    try:
        _install_time_budget(conn)
        conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


def _dump_all_tables(db_path):
    """Returns {table_name: sorted_rows} for every table in db_path, for whole-database
    comparison after a write statement. Rows are sorted in Python (not via SQL ORDER BY) so this
    works regardless of the table's column count/types, and is independent of insertion order or
    internal rowid assignment."""
    conn = sqlite3.connect('file:%s?mode=ro' % db_path, uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        state = {}
        for table in tables:
            rows = conn.execute('SELECT * FROM "%s"' % table.replace('"', '""')).fetchall()
            state[table] = sorted(rows, key=_sort_key)
        return state
    finally:
        conn.close()


def _compare_results(student, expected, order_matters=False,
                     compare_columns=False, float_tolerance=0.001,
                     point_value=1):
    student_cols, student_rows = student
    expected_cols, expected_rows = expected

    if len(student_cols) != len(expected_cols):
        return CheckerResult(False, 0,
                             feedback="Wrong number of columns: got %d, expected %d" %
                                      (len(student_cols), len(expected_cols)))

    if compare_columns:
        s_lower = [c.lower() for c in student_cols]
        e_lower = [c.lower() for c in expected_cols]
        if s_lower != e_lower:
            return CheckerResult(False, 0,
                                 feedback="Column names don't match. Got: %s, Expected: %s" %
                                          (', '.join(student_cols), ', '.join(expected_cols)))

    if len(student_rows) != len(expected_rows):
        return CheckerResult(False, 0,
                             feedback="Wrong number of rows: got %d, expected %d" %
                                      (len(student_rows), len(expected_rows)))

    if order_matters:
        for i, (s_row, e_row) in enumerate(zip(student_rows, expected_rows)):
            if not _rows_equal(s_row, e_row, float_tolerance):
                return CheckerResult(False, 0, feedback="Row %d differs" % (i + 1))
    else:
        s_sorted = sorted(student_rows, key=_sort_key)
        e_sorted = sorted(expected_rows, key=_sort_key)
        for s_row, e_row in zip(s_sorted, e_sorted):
            if not _rows_equal(s_row, e_row, float_tolerance):
                return CheckerResult(False, 0, feedback="Result sets differ (comparing unordered)")

    return CheckerResult(True, point_value, feedback="")


def _sort_key(row):
    return tuple((0, '') if v is None else (1, str(v)) for v in row)


def _rows_equal(row1, row2, tolerance):
    if len(row1) != len(row2):
        return False
    for v1, v2 in zip(row1, row2):
        if v1 is None and v2 is None:
            continue
        if v1 is None or v2 is None:
            return False
        if isinstance(v1, float) or isinstance(v2, float):
            try:
                if abs(float(v1) - float(v2)) > tolerance:
                    return False
            except (ValueError, TypeError):
                return False
        elif v1 != v2:
            return False
    return True