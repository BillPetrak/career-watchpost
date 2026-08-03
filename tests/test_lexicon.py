"""Tests for the data-driven technical-skill lexicon and boundary-safe matching."""

from careeros.ranking.lexicon import term_matches


def test_c_matches_standalone_token() -> None:
    assert term_matches("C", "We write C programming.")
    assert term_matches("C", "C and C++ both listed.")
    assert term_matches("c", "experience in c")


def test_c_never_matches_inside_other_words() -> None:
    assert not term_matches("C", "Experience with C++ and C#.")
    assert not term_matches("C", "C/C++ developers wanted.")
    assert not term_matches("C", "a cider by the creek")
    assert not term_matches("C", "Cplusplus is not a language")


def test_cpp_matches() -> None:
    assert term_matches("C++", "Write C++ code.")
    assert term_matches("c++", "modern c++ features")
    assert term_matches("cpp", "cpp codebase")


def test_csharp_matches() -> None:
    assert term_matches("C#", "C# and .NET developer.")
    assert term_matches("csharp", "csharp scripting")


def test_dotnet_aliases() -> None:
    assert term_matches(".NET", "Full-stack .NET engineer.")
    assert term_matches("dotnet", "dotnet core services")
    assert not term_matches(".NET", "asp.net web forms")


def test_nodejs_aliases() -> None:
    assert term_matches("Node.js", "Node.js backend")
    assert term_matches("node", "node development")
    assert term_matches("nodejs", "nodejs services")


def test_javascript_js_standalone_only() -> None:
    assert term_matches("JavaScript", "JavaScript frontend")
    assert term_matches("js", "js and css")
    assert not term_matches("js", "json payloads")
    assert not term_matches("js", "nodejs services")


def test_typescript_ts_standalone_only() -> None:
    assert term_matches("TypeScript", "TypeScript codebase")
    assert term_matches("ts", "ts tests")
    assert not term_matches("ts", "cats are nice")


def test_react_aliases() -> None:
    assert term_matches("React", "React UI")
    assert term_matches("react.js", "react.js components")
    assert term_matches("reactjs", "reactjs developer")


def test_ci_cd() -> None:
    assert term_matches("CI/CD", "CI/CD pipelines")
    assert term_matches("ci/cd", "ci/cd setup")
    assert term_matches("cicd", "cicd automation")


def test_aws() -> None:
    assert term_matches("AWS", "AWS infrastructure")
    assert term_matches("amazon web services", "Amazon Web Services certified")


def test_postgres_aliases() -> None:
    assert term_matches("PostgreSQL", "PostgreSQL database")
    assert term_matches("postgres", "manage postgres instances")
    assert term_matches("PostgreSQL", "querying postgresql")
    assert term_matches("psql", "psql scripts")


def test_sql_standalone_only() -> None:
    assert term_matches("SQL", "advanced SQL queries")
    assert not term_matches("SQL", "SQLite embedded")
    assert not term_matches("SQL", "postgresql administration")


def test_fastapi() -> None:
    assert term_matches("FastAPI", "FastAPI services")
    assert term_matches("fastapi", "build fastapi apps")
    assert term_matches("fast api", "Fast API endpoints")


def test_case_insensitive() -> None:
    assert term_matches("PostgreSQL", "POSTGRESQL")
    assert term_matches("c++", "C++")


def test_generic_phrase_fallback() -> None:
    assert term_matches("Machine Learning", "We use machine learning a lot.")
    assert term_matches("Data Analysis", "data analysis with pandas")
    assert not term_matches("Machine Learning", "machines learn things")


def test_unicode_folding() -> None:
    assert term_matches("Cafe", "we serve caf\u00e9 code")


def test_empty_term() -> None:
    assert not term_matches("", "anything here")
