# Manual test checklist

`python -m unittest discover -s tests -v` does **not** cover these items; they cannot be imitated in the test environment. No item counts as passed until it is observed. Run each item in a **temporary Git repository** (not in a real project) and write the outcome in the "RESULT" field with the date. All results are currently `not observed`.

General preparation (PowerShell):

```powershell
$env:COPCULER_STATE_DIR = "$env:TEMP\copculer-manual-state"
$d = "$env:TEMP\copculer-manual-try"; New-Item -ItemType Directory $d | Out-Null; Set-Location $d
git init -q; Set-Content a.py "a`n# note`nc`n"; git add -A; git -c user.name=t -c user.email=t@t commit -q -m initial
```

Keep the plan file outside the repo (in `$env:TEMP`). In every item run the flow `snapshot` → plan → `review` → `apply` → `undo` and verify (for example with `Get-FileHash`) that the bytes equal the starting bytes. Candidates must be `REVIEW_COMPLETE` to be applied, so use comment-only deletions or give correct `symbols`.

## 1. OneDrive
Create the repo inside a OneDrive folder.
- [ ] Flow works on "always keep on this device" files. RESULT: not observed
- [ ] "Online only" (cloud) file: `snapshot` and `apply` give a meaningful error or download the file and process it correctly; no half-written state remains. RESULT: not observed
- [ ] During synchronization, `undo` after `apply` restores the bytes exactly. RESULT: not observed

## 2. CRLF and line-ending variations
Automated tests only try `core.autocrlf=false` with no `.gitattributes`; that does **not** cover the other settings. In each combination delete one line from a file with CRLF line endings, `apply`, compare bytes, `undo`, compare bytes.

| `.gitattributes` | `core.autocrlf` | apply | undo | RESULT |
| --- | --- | --- | --- | --- |
| none | `true` | | | not observed |
| none | `input` | | | not observed |
| none | `false` | | | not observed |
| `* text=auto` | `true` / `input` / `false` | | | not observed |
| `* text eol=crlf` | `true` / `input` / `false` | | | not observed |
| `* -text` | `true` / `input` / `false` | | | not observed |

Note: the `base_blob` record can be `null` under CRLF conversions (the Git cross-check is then not possible); that is expected, and `undo` must work from the backup.

## 3. LFS and filters
- [ ] A file tracked with Git LFS: it is out of scope in `snapshot` (pointer file versus content) and `apply` does not corrupt it. RESULT: not observed
- [ ] A repo with a repo-local `filter.*.clean|smudge|process` or `diff.*.textconv` defined: every command refuses with "run commands on files" and no filter command runs (by design, so there is no apply/undo to check). RESULT: not observed
- [ ] A repo with a global (user-level) LFS or filter setup but no repo-local filter: the tool ignores global config; after `apply`, `git diff` shows no unexpected change and `undo` restores the bytes. RESULT: not observed
- [ ] A patch whose context contains ANSI escape sequences or a right-to-left override: the confirmation screen shows them as `\x1b` / `‮` text, and your real terminal is not repainted. RESULT: not observed

## 4. File permissions
- [ ] On POSIX, delete a file with the executable bit (`chmod +x`) using `delete_file`, then `undo`: mode `100755` is preserved. RESULT: not observed (the bit cannot be observed on Windows)
- [ ] On POSIX, the state directory mode is `0700` (the automated test is skipped on Windows). RESULT: not observed

## 5. Real terminal confirmation
- [ ] `apply` / `undo` / `recover` ask for the `APPLY|UNDO|RECOVER <digest>` line in an interactive terminal; a wrong line stops with "Confirmation did not match". In automated tests `confirm` is patched. RESULT: not observed
- [ ] With output redirected (pipe/file) or stdin not a terminal, the same commands are refused with "Enter the confirmation in your own interactive terminal". RESULT: not observed

## 6. Links (Windows)
- [ ] On an account with the privilege (developer mode / administrator) create a symlink: `snapshot` reason is `link`. The automated test is skipped without this privilege. RESULT: not observed
- [ ] A path containing an NTFS junction / reparse point is out of scope as `link`. RESULT: not observed

## 7. Real partial failure
- [ ] During `apply`, lock one of the files in another program (Windows) or make it read-only; the journal becomes `partial`, and `recover --backward` and `--forward` work once the lock is released. In the automated test the failure is produced by patching `apply_patch`. RESULT: not observed

## 8. Python version
- [ ] With Python 3.11+, the `.toml` syntax result is `PASSED`/`ERROR` (on 3.10 it is `UNVERIFIED`; the related test is skipped on 3.10). RESULT: not observed
- [ ] With Python 3.12+, `type X = int` and type-parameter syntax parse, and the 3.12-only test runs. RESULT: not observed
