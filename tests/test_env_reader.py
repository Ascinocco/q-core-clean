"""nix/env-reader.sh: how q-core-cli reads the service's environment file.

Values are invented. The reader must take values literally (never run them),
skip malformed lines with a warning that doesn't repeat the line, and keep
going under `set -eu`, as q-core-cli runs.
"""
import json
import subprocess
from pathlib import Path

READER = Path(__file__).resolve().parent.parent / "nix" / "env-reader.sh"


def _read(tmp_path, text):
    env_file = tmp_path / "q-core.env"
    env_file.write_text(text)
    script = (
        f"set -eu; . {READER}; read_env_file {env_file}; "
        "python3 -c 'import json, os; print(json.dumps({k: v for k, v in os.environ.items() if k.startswith(\"T_\")}))'"
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout), result.stderr


def test_values_are_literal_and_quotes_are_removed_once(tmp_path):
    exported, _ = _read(tmp_path, 'T_PLAIN=plain\nT_SHELL=two words $x `id`\nT_DQ="quoted"\nT_SQ=\'single\'\nT_EQ=a=b\n')
    assert exported == {"T_PLAIN": "plain", "T_SHELL": "two words $x `id`", "T_DQ": "quoted",
                        "T_SQ": "single", "T_EQ": "a=b"}


def test_comments_blank_and_indented_lines(tmp_path):
    exported, stderr = _read(tmp_path, "# comment\n; also a comment\n\n   T_INDENTED=yes\n\t# indented comment\n")
    assert exported == {"T_INDENTED": "yes"} and stderr == ""


def test_malformed_lines_are_skipped_with_a_warning_that_does_not_repeat_them(tmp_path):
    exported, stderr = _read(tmp_path, "T_NOEQ_invented_secret\nT_BAD KEY=invented_secret\n1T_DIGIT=x\nT_OK=1\n")
    assert exported == {"T_OK": "1"}
    assert stderr.count("skipped an environment-file line") == 3
    assert "invented_secret" not in stderr


def test_a_last_line_without_a_newline_is_read(tmp_path):
    exported, _ = _read(tmp_path, "T_LAST=end")
    assert exported == {"T_LAST": "end"}
