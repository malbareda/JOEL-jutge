"""
Mongo Checker for DMOJ

Executes both the expected Mongo call and the student's Mongo call against an in-memory
mongomock database (loaded from a JSON file), then compares the results.

Usage in init.yml:
    checker:
        name: mongo.py
        args:
            db_file: employees.json         # filename of the DB inside the problem directory
            question_index: 2               # optional: which multi-question answer to grade (see below)

The "database" is a plain JSON file: {"collection_name": [doc1, doc2, ...], ...}.

The student submits a restricted Mongo-shell-like call as their "source code" (language: TEXT):
    db.<collection>.<method>(<args>[, <args>])
Arguments accept real Mongo-shell object-literal syntax: unquoted keys (`{ _id: 7 }`), single- or
double-quoted strings, nested objects/arrays, numbers, true/false/null -- parsed by a small
hand-rolled recursive-descent parser (_JsonLikeParser, below), not Python's json module directly
(though any argument that already IS strict JSON parses identically, since JSON is a subset of this
grammar). Still deliberately NOT supported: function-call-style constructs like `ObjectId(...)`,
`ISODate(...)`, ``new Date()`` etc. -- these aren't values this grammar's parser can produce, so
they fail to parse with a clear error, same as before.

Statement type (find / aggregate / insertOne / insertMany / updateOne / updateMany -- NOT delete*,
deliberately unsupported for now, same restriction as the SQL checker) is detected automatically
from the reference call stored in the case's .out file -- no extra configuration needed. A `find`
or `aggregate` case is graded exactly like a SQL SELECT (read-only, compare result sets) --
`aggregate` takes a single JSON array (the pipeline) as its only argument, e.g.
`db.orders.aggregate([{"$group": {"_id": "$customer", "total": {"$sum": "$amount"}}}])`. An
insert*/update* case is graded by applying the call to a **fresh, isolated
mongomock.MongoClient()** loaded from the same JSON file and comparing the resulting content of
every collection against a second, independently loaded client where the *reference* call was
applied -- the original .json file on disk is only ever opened for reading; see _load_client().
Because the whole database is compared, a reference insert/update should specify an explicit
"_id" for any document it touches rather than relying on an auto-generated ObjectId, or the
comparison could spuriously fail (mongomock, like real Mongo, assigns a fresh random ObjectId per
call when "_id" is omitted).

`aggregate` pipelines are read-only in this checker: `$out`/`$merge` (the two stages that would
write to a collection) are rejected the same way `$where`/`$function`/`$accumulator` are --
matching the "SELECT-only" spirit of a read case, not because mongomock's in-memory `$out`/`$merge`
could ever escape the throwaway client it runs in.

Multi-question problems: identical mechanism to the SQL checker, reusing the same "-- @@Q<n>@@"
marker convention the web app's submission form already produces.
"""
import json
import os
import re
import threading
import time
import traceback

import mongomock

from dmoj.judgeenv import get_problem_root
from dmoj.result import CheckerResult

_QUESTION_MARKER_RE = re.compile(r'^[ \t]*--[ \t]*@@Q(\d+)@@[ \t]*\r?$', re.MULTILINE)

# Statement types a student may ever submit. deleteOne/deleteMany/remove/drop/dropDatabase/
# replaceOne are deliberately NOT included yet, same restriction as the SQL checker.
_ALLOWED_METHODS = ('find', 'aggregate', 'insertOne', 'insertMany', 'updateOne', 'updateMany')

# Methods that only ever read (never mutate a collection) -- graded like a SQL SELECT.
_READ_METHODS = ('find', 'aggregate')

_CALL_RE = re.compile(r'^db\.(\w+)\.(\w+)\((.*)\)\s*;?\s*$', re.DOTALL)

# Keys that would let a document reach into JavaScript evaluation. mongomock already refuses all
# of these structurally ($where -> NotImplementedError, $function/$accumulator -> OperationFailure,
# verified empirically before designing this checker) -- this is a second, independent net that
# gives a clean error message instead of leaking an internal mongomock exception. $out/$merge are
# the two aggregation stages that write to a collection -- rejected to keep `aggregate` read-only,
# matching the "SELECT-only" spirit of a read case (not because they could escape the throwaway
# in-memory client they'd run in -- they can't, there's nothing to escape to).
_FORBIDDEN_KEYS = ('$where', '$function', '$accumulator', '$expr', '$out', '$merge')

# Prepended to feedback for a truly-forbidden method/operator rejection (NOT for "wrong method for
# this question", which is an ordinary mistake) so judge/bridge/judge_handler.py's on_test_case can
# flag this case as a deliberate destructive/escaping attempt with the 'SEC' status instead of
# 'WA'. Must match the constant of the same name in dmoj/checkers/sql.py and in that bridge file
# exactly.
SECURITY_VIOLATION_MARKER = '@@SECVIOL@@'

# Wall-clock budget, mirroring the SQL checker's _QUERY_TIME_BUDGET_SECONDS. Unlike SQLite's
# set_progress_handler, mongomock has no clean way to interrupt an operation that's already
# running, so this can't truly cancel a runaway query -- see _run_with_timeout() below for how it's
# enforced instead (a join(timeout=...) on a throwaway thread). `aggregate` widens the realistic
# blast radius versus find/insert*/update* (a pathological $lookup or a long pipeline over a big
# collection could be slow), which is the main reason this budget exists.
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
          db_file='employees.json', order_matters=False, point_value=1,
          problem_id=None, question_index=None, **kwargs):
    """
    DMOJ checker interface.

    process_output: stdout of the student's program (= the Mongo call, via TEXT executor)
    judge_output:   content of the .out file (= the correct Mongo call)
    problem_id:     passed by DMOJ in kwargs, used to locate the DB file
    question_index: passed by DMOJ if this problem asks more than one question (see module docstring)
    """

    student_call_full = _to_str(process_output).strip()
    expected_call = _to_str(judge_output).strip()

    if not student_call_full:
        return CheckerResult(False, 0, feedback="Empty submission - no Mongo query provided")

    if question_index is not None and _QUESTION_MARKER_RE.search(student_call_full):
        segment = _extract_question_segment(student_call_full, int(question_index))
        if segment is None:
            return CheckerResult(False, 0,
                                 feedback="No answer found for question %d" % int(question_index))
        student_call = segment
    else:
        student_call = student_call_full

    if not student_call:
        return CheckerResult(False, 0, feedback="Empty submission - no Mongo query provided")

    if not expected_call:
        return CheckerResult(False, 0, feedback="No expected Mongo call configured (check .out file)")

    db_path = None
    if problem_id:
        try:
            problem_root = get_problem_root(problem_id)
            db_path = os.path.join(problem_root, str(db_file))
        except Exception:
            pass

    if not db_path or not os.path.exists(db_path):
        db_path = str(db_file)

    if not os.path.exists(db_path):
        return CheckerResult(False, 0, feedback="Database file not found: %s" % db_file)

    try:
        expected_parsed = _parse_mongo_call(expected_call)
    except _MongoCallError:
        return CheckerResult(False, 0,
                             feedback="Problem configuration error: reference Mongo call could not "
                                      "be parsed")
    if expected_parsed.method not in _ALLOWED_METHODS:
        return CheckerResult(False, 0,
                             feedback="Problem configuration error: reference Mongo call uses an "
                                      "unrecognized method")

    try:
        student_parsed = _parse_mongo_call(student_call)
    except _MongoCallError as e:
        return CheckerResult(False, 0, feedback=str(e))

    security_msg = _security_check(student_parsed, expected_parsed.method)
    if security_msg:
        return CheckerResult(False, 0, feedback=security_msg)

    if expected_parsed.method in _READ_METHODS:
        return _check_read(db_path, student_parsed, expected_parsed,
                           order_matters=bool(order_matters), point_value=float(point_value))
    else:
        return _check_write(db_path, student_parsed, expected_parsed, point_value=float(point_value))


class _MongoCallError(Exception):
    pass


class _MongoTimeoutError(_MongoCallError):
    pass


def _run_with_timeout(func, *args, **kwargs):
    """Runs func(*args, **kwargs) in a throwaway thread and enforces _QUERY_TIME_BUDGET_SECONDS,
    raising _MongoTimeoutError if it's exceeded.

    mongomock has no equivalent of SQLite's set_progress_handler, so a pathological query (e.g. a
    huge $lookup, or an aggregate pipeline over a big collection) cannot be interrupted mid-flight --
    this only stops *waiting* for it. On timeout the worker thread is abandoned (daemon=True, so it
    can never keep the judge process itself alive) and whichever mongomock client `func` was
    operating on must never be touched again afterwards, since the abandoned thread may still be
    reading or writing to it in the background. Every caller of this function respects that: on a
    timeout (or any other exception) the checker returns immediately without any further access to
    that client, and each call site always operates on its own freshly loaded client to begin with
    (see _load_client), so no two threads ever share one."""
    outcome = []

    def _target():
        try:
            outcome.append((True, func(*args, **kwargs)))
        except BaseException as e:
            outcome.append((False, e))

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(_QUERY_TIME_BUDGET_SECONDS)
    if thread.is_alive():
        raise _MongoTimeoutError(
            "Query took too long to run (over %d seconds) -- check for an inefficient or "
            "unbounded query" % _QUERY_TIME_BUDGET_SECONDS)
    ok, value = outcome[0]
    if not ok:
        raise value
    return value


class _ParsedCall(object):
    def __init__(self, collection, method, args):
        self.collection = collection
        self.method = method
        self.args = args


class _JsonLikeParser(object):
    """Recursive-descent parser for Mongo-shell object-literal syntax: like JSON, but object keys
    may be bare identifiers (`_id`, `$set`, ...) instead of quoted strings, and strings may use
    single quotes as well as double quotes. Builds only plain dict/list/str/int/float/bool/None --
    no eval() anywhere, so there is no way for a submission to reach arbitrary Python execution.
    Deliberately has no notion of a function call (`Foo(...)`), so constructs like `ObjectId(7)`
    fail to parse as a value, same restriction as before this parser existed."""

    _WS = ' \t\n\r'
    _IDENT_RE = re.compile(r'[A-Za-z_$][A-Za-z0-9_$]*')
    _NUMBER_RE = re.compile(r'-?\d+(\.\d+)?([eE][+-]?\d+)?')
    _ESCAPES = {'"': '"', "'": "'", '\\': '\\', '/': '/',
               'n': '\n', 't': '\t', 'r': '\r', 'b': '\b', 'f': '\f'}

    def __init__(self, text):
        self.text = text
        self.pos = 0
        self.n = len(text)

    def _skip_ws(self):
        while self.pos < self.n and self.text[self.pos] in self._WS:
            self.pos += 1

    def parse_top_level_values(self):
        """Parses zero or more comma-separated values until the end of the text (used for the
        argument list inside `db.<collection>.<method>(...)`, which is 0-2 values)."""
        values = []
        self._skip_ws()
        while self.pos < self.n:
            values.append(self.parse_value())
            self._skip_ws()
            if self.pos < self.n and self.text[self.pos] == ',':
                self.pos += 1
                self._skip_ws()
        return values

    def parse_value(self):
        self._skip_ws()
        if self.pos >= self.n:
            raise _MongoCallError("Unexpected end of input while parsing arguments")
        ch = self.text[self.pos]
        if ch == '{':
            return self._parse_object()
        if ch == '[':
            return self._parse_array()
        if ch == '"' or ch == "'":
            return self._parse_string()
        if ch == '-' or ch.isdigit():
            return self._parse_number()
        return self._parse_keyword()

    def _parse_object(self):
        self.pos += 1  # consume '{'
        obj = {}
        self._skip_ws()
        if self.pos < self.n and self.text[self.pos] == '}':
            self.pos += 1
            return obj
        while True:
            self._skip_ws()
            key = self._parse_key()
            self._skip_ws()
            if self.pos >= self.n or self.text[self.pos] != ':':
                raise _MongoCallError("Expected ':' after object key %r" % key)
            self.pos += 1
            obj[key] = self.parse_value()
            self._skip_ws()
            if self.pos < self.n and self.text[self.pos] == ',':
                self.pos += 1
                self._skip_ws()
                if self.pos < self.n and self.text[self.pos] == '}':  # trailing comma
                    self.pos += 1
                    return obj
                continue
            if self.pos < self.n and self.text[self.pos] == '}':
                self.pos += 1
                return obj
            raise _MongoCallError("Expected ',' or '}' in object")

    def _parse_key(self):
        if self.pos >= self.n:
            raise _MongoCallError("Expected an object key")
        ch = self.text[self.pos]
        if ch == '"' or ch == "'":
            return self._parse_string()
        match = self._IDENT_RE.match(self.text, self.pos)
        if not match:
            raise _MongoCallError("Invalid object key near: %r" % self.text[self.pos:self.pos + 20])
        self.pos = match.end()
        return match.group(0)

    def _parse_array(self):
        self.pos += 1  # consume '['
        arr = []
        self._skip_ws()
        if self.pos < self.n and self.text[self.pos] == ']':
            self.pos += 1
            return arr
        while True:
            arr.append(self.parse_value())
            self._skip_ws()
            if self.pos < self.n and self.text[self.pos] == ',':
                self.pos += 1
                self._skip_ws()
                if self.pos < self.n and self.text[self.pos] == ']':  # trailing comma
                    self.pos += 1
                    return arr
                continue
            if self.pos < self.n and self.text[self.pos] == ']':
                self.pos += 1
                return arr
            raise _MongoCallError("Expected ',' or ']' in array")

    def _parse_string(self):
        quote = self.text[self.pos]
        self.pos += 1
        chars = []
        while True:
            if self.pos >= self.n:
                raise _MongoCallError("Unterminated string literal")
            ch = self.text[self.pos]
            if ch == quote:
                self.pos += 1
                return ''.join(chars)
            if ch == '\\':
                self.pos += 1
                if self.pos >= self.n:
                    raise _MongoCallError("Unterminated escape sequence in string")
                esc = self.text[self.pos]
                if esc in self._ESCAPES:
                    chars.append(self._ESCAPES[esc])
                    self.pos += 1
                elif esc == 'u':
                    hex_digits = self.text[self.pos + 1:self.pos + 5]
                    if len(hex_digits) != 4:
                        raise _MongoCallError("Invalid \\u escape in string")
                    chars.append(chr(int(hex_digits, 16)))
                    self.pos += 5
                else:
                    raise _MongoCallError("Invalid escape sequence: \\%s" % esc)
                continue
            chars.append(ch)
            self.pos += 1

    def _parse_number(self):
        match = self._NUMBER_RE.match(self.text, self.pos)
        if not match:
            raise _MongoCallError("Invalid number near: %r" % self.text[self.pos:self.pos + 20])
        raw = match.group(0)
        self.pos = match.end()
        return float(raw) if ('.' in raw or 'e' in raw or 'E' in raw) else int(raw)

    def _parse_keyword(self):
        for keyword, value in (('true', True), ('false', False), ('null', None)):
            if self.text[self.pos:self.pos + len(keyword)] == keyword:
                self.pos += len(keyword)
                return value
        raise _MongoCallError("Could not parse argument near: %r -- expected a JSON-like value "
                              "(object, array, string, number, true/false/null)" %
                              self.text[self.pos:self.pos + 20])


def _parse_mongo_call(text):
    """Parses `db.<collection>.<method>(<args>[, <args>])` into a _ParsedCall. Arguments are
    parsed with _JsonLikeParser, which accepts both strict JSON and real Mongo-shell object-literal
    syntax (unquoted keys, single-quoted strings) -- see the module docstring."""
    text = text.strip()
    match = _CALL_RE.match(text)
    if not match:
        raise _MongoCallError(
            "Could not parse Mongo call. Expected format: db.<collection>.<method>(<arguments>)")

    collection, method, raw_args = match.group(1), match.group(2), match.group(3).strip()

    args = _JsonLikeParser(raw_args).parse_top_level_values()

    if len(args) > 2:
        raise _MongoCallError("Too many arguments -- at most a filter/document and a projection/"
                              "update are expected")

    return _ParsedCall(collection, method, args)


def _contains_forbidden_key(value):
    if isinstance(value, dict):
        for key, sub in value.items():
            if key in _FORBIDDEN_KEYS:
                return True
            if _contains_forbidden_key(sub):
                return True
    elif isinstance(value, list):
        for item in value:
            if _contains_forbidden_key(item):
                return True
    return False


def _security_check(parsed, allowed_method):
    if parsed.method not in _ALLOWED_METHODS:
        return SECURITY_VIOLATION_MARKER + ("This Mongo method is not allowed: %s" % parsed.method)
    if parsed.method != allowed_method:
        return "Expected a %s() call for this question, found: %s()" % (allowed_method, parsed.method)
    for arg in parsed.args:
        if _contains_forbidden_key(arg):
            return SECURITY_VIOLATION_MARKER + "This query uses an operator that is not allowed"
    return None


def _load_client(db_path):
    """Loads db_path (a plain read-only filesystem read -- db_path is never opened for writing)
    into a brand new, isolated mongomock.MongoClient(). Each call returns its own client instance
    that shares nothing with any other -- there is no on-disk "copy" step like the SQL checker's
    _make_temp_copy, because mongomock databases live only in memory to begin with."""
    with open(db_path, 'r') as f:
        database = json.load(f)
    client = mongomock.MongoClient()
    db = client['db']
    for collection_name, documents in database.items():
        if documents:
            db[collection_name].insert_many([dict(doc) for doc in documents])
    return db


def _check_read(db_path, student_parsed, expected_parsed, order_matters, point_value):
    try:
        expected_db = _load_client(db_path)
        expected_result = _run_with_timeout(_run_read, expected_db, expected_parsed)
    except Exception as e:
        return CheckerResult(False, 0,
                             feedback="Internal error with expected query: %s" % str(e),
                             extended_feedback=traceback.format_exc())

    try:
        student_db = _load_client(db_path)
        student_result = _run_with_timeout(_run_read, student_db, student_parsed)
    except _MongoCallError as e:
        return CheckerResult(False, 0, feedback=str(e))
    except Exception as e:
        return CheckerResult(False, 0, feedback="Error executing your query: %s" % str(e))

    return _compare_documents(student_result, expected_result, order_matters, point_value)


def _run_read(db, parsed):
    if parsed.collection not in db.list_collection_names():
        raise _MongoCallError("No such collection: %s" % parsed.collection)
    if parsed.method == 'find':
        filter_ = parsed.args[0] if len(parsed.args) >= 1 else {}
        projection = parsed.args[1] if len(parsed.args) >= 2 else None
        cursor = db[parsed.collection].find(filter_, projection) if projection is not None \
            else db[parsed.collection].find(filter_)
        return list(cursor)
    elif parsed.method == 'aggregate':
        if len(parsed.args) != 1 or not isinstance(parsed.args[0], list):
            raise _MongoCallError("aggregate() expects a single JSON array (the pipeline)")
        return list(db[parsed.collection].aggregate(parsed.args[0]))
    else:
        raise _MongoCallError("This Mongo method is not allowed: %s" % parsed.method)


def _check_write(db_path, student_parsed, expected_parsed, point_value):
    try:
        student_db = _load_client(db_path)
        try:
            _run_with_timeout(_run_write, student_db, student_parsed)
        except _MongoCallError as e:
            return CheckerResult(False, 0, feedback=str(e))
        except Exception as e:
            return CheckerResult(False, 0, feedback="Error executing your query: %s" % str(e))
        student_state = _dump_all_collections(student_db)

        reference_db = _load_client(db_path)
        _run_with_timeout(_run_write, reference_db, expected_parsed)
        expected_state = _dump_all_collections(reference_db)
    except Exception as e:
        return CheckerResult(False, 0,
                             feedback="Internal error grading this submission: %s" % str(e),
                             extended_feedback=traceback.format_exc())

    if student_state == expected_state:
        return CheckerResult(True, point_value, feedback="")
    return CheckerResult(False, 0,
                         feedback="The database's final content does not match what was expected "
                                  "after running your statement.")


def _run_write(db, parsed):
    collection = db[parsed.collection]
    if parsed.method == 'insertOne':
        if len(parsed.args) != 1 or not isinstance(parsed.args[0], dict):
            raise _MongoCallError("insertOne() expects a single JSON document")
        collection.insert_one(parsed.args[0])
    elif parsed.method == 'insertMany':
        if len(parsed.args) != 1 or not isinstance(parsed.args[0], list):
            raise _MongoCallError("insertMany() expects a single JSON array of documents")
        collection.insert_many(parsed.args[0])
    elif parsed.method == 'updateOne':
        if len(parsed.args) != 2 or not isinstance(parsed.args[0], dict) or not isinstance(parsed.args[1], dict):
            raise _MongoCallError("updateOne() expects a filter and an update document")
        collection.update_one(parsed.args[0], parsed.args[1])
    elif parsed.method == 'updateMany':
        if len(parsed.args) != 2 or not isinstance(parsed.args[0], dict) or not isinstance(parsed.args[1], dict):
            raise _MongoCallError("updateMany() expects a filter and an update document")
        collection.update_many(parsed.args[0], parsed.args[1])
    else:
        raise _MongoCallError("This Mongo method is not allowed: %s" % parsed.method)


def _dump_all_collections(db):
    """Returns {collection_name: sorted_canonical_json_docs} for every collection in db, for
    whole-database comparison after a write call. Documents are serialized to their canonical JSON
    representation (sorted keys) and then sorted as strings, since a plain Python dict is not
    orderable -- independent of insertion order or internal id assignment order."""
    state = {}
    for name in db.list_collection_names():
        docs = list(db[name].find({}))
        canonical = sorted(json.dumps(doc, sort_keys=True, default=str) for doc in docs)
        state[name] = canonical
    return state


def _to_str(data):
    if isinstance(data, bytes):
        return data.decode('utf-8', errors='replace')
    return str(data) if data else ''


def _canonical(doc):
    return json.dumps(doc, sort_keys=True, default=str)


def _compare_documents(student, expected, order_matters, point_value):
    if len(student) != len(expected):
        return CheckerResult(False, 0,
                             feedback="Wrong number of documents: got %d, expected %d" %
                                      (len(student), len(expected)))

    if order_matters:
        for i, (s_doc, e_doc) in enumerate(zip(student, expected)):
            if _canonical(s_doc) != _canonical(e_doc):
                return CheckerResult(False, 0, feedback="Document %d differs" % (i + 1))
    else:
        s_sorted = sorted(student, key=_canonical)
        e_sorted = sorted(expected, key=_canonical)
        for s_doc, e_doc in zip(s_sorted, e_sorted):
            if _canonical(s_doc) != _canonical(e_doc):
                return CheckerResult(False, 0, feedback="Result sets differ (comparing unordered)")

    return CheckerResult(True, point_value, feedback="")
