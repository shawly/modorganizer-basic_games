"""Merges .settings fragments shipped by mods into the live Witcher 3 settings files.

Each merged option (or input binding group) is recorded in a JSON manifest next to the
targets with its pre-merge "baseline" and the "written" value. On every run the live value
is classified against that record:
  ours  - still what we wrote; safe to update or roll back
  reset - game/user restored the baseline or removed it; re-apply
  user  - changed by user/game; left untouched
Entries of mods that are no longer active are rolled back to their baseline.
"""
from __future__ import annotations

import functools
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

FRAGMENT_DIR_NAME: str = "W3_SettingsFragments"
MANIFEST_FILENAME: str = "witcher3_settings_merge.json"
BACKUP_SUFFIX: str = ".mo2_backup"

# Fragment kind -> target files it is merged into
KIND_TARGETS = {"user": ("user.settings", "dx12user.settings"), "input": ("input.settings",)}

Identity = Tuple[str, ...]
Items = List[Tuple[str, str]]
Log = Callable[[str, bool], None]

# Empty fragments are logged only once per MO2 session
_LOGGED_EMPTY_FRAGMENTS: Set[str] = set()


@dataclass
class Entry:
    """A single key=value line within a section."""
    section: str
    key: str
    value: str


@dataclass
class ParsedSettingsFile:
    """Section names in file order (first spelling wins) and all entries."""
    sections: List[str] = field(default_factory=list)
    entries: List[Entry] = field(default_factory=list)


@dataclass
class Claim:
    """The value a fragment wants for one identity; later (higher priority) claims replace earlier ones."""
    identity: Identity
    section: str
    items: Items
    source: str
    frag_path: str


def fragment_kind(name: str) -> Optional[str]:
    """Returns 'input' or 'user' for fragment filenames (*input.settings / *user.settings, optionally .txt), else None."""
    nl = name.strip().lower()
    if not nl.endswith((".settings", ".txt")):
        return None
    if "input.settings" in nl:
        return "input"
    if "user.settings" in nl:
        return "user"
    return None


def unique_fragment_name(existing_names: Set[str] | List[str], filename: str, origin: str) -> str:
    """Prefixes the fragment with its origin mod folder and adds a counter on collision; the kind suffix is kept."""
    existing = {n.casefold() for n in existing_names}
    base = f"{origin}.{filename}" if origin and not filename.casefold().startswith(origin.casefold()) else filename
    candidate, idx = base, 1
    while candidate.casefold() in existing:
        candidate = f"{idx}_{base}"
        idx += 1
    return candidate


def _describe_identity(ident: Identity) -> str:
    """Readable identity for log messages."""
    return f"[{ident[1]}] {ident[3]} ({ident[2]})" if ident[0] == "binding" else f"[{ident[1]}] {ident[2]}"


def _normalize_val(val: str, is_binding: bool) -> str:
    """Normalizes a value so that e.g. '1.0'/'1', 'True'/'true' or whitespace in bindings compare equal."""
    if is_binding:
        return "".join(val.split()).casefold()
    v = val.strip().casefold()
    if v in ("true", "false"):
        return v
    try:
        f = float(v)
        return str(int(f)) if f.is_integer() else str(f)
    except ValueError:
        return v


def _same(a: Items, b: Items, is_bind: bool) -> bool:
    """Order-independent, normalized comparison of two item lists."""
    def norm(items: Items) -> Items:
        return sorted((k.strip().casefold(), _normalize_val(v, is_bind)) for k, v in items)
    return norm(a) == norm(b)


def _format_items(items: Items) -> str:
    return ", ".join(f"{k}={v}" for k, v in items)


def _atomic_write(path: Path, data: bytes) -> None:
    """Writes via a temp file and os.replace so the target is never left half-written."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _parse_settings_text(text: str) -> ParsedSettingsFile:
    """Parses INI-style text; comments, blank lines and entries outside a section are dropped."""
    parsed = ParsedSettingsFile()
    current_section = ""
    seen: Set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1]
            if current_section.casefold() not in seen:
                seen.add(current_section.casefold())
                parsed.sections.append(current_section)
        elif "=" in line and current_section:
            k, v = line.split("=", 1)
            parsed.entries.append(Entry(current_section, k.strip(), v.strip()))
    return parsed


def _load_parsed(path: Path) -> Tuple[bytes, str, str, ParsedSettingsFile]:
    """Returns (raw bytes, detected encoding, dominant newline, parsed content); a missing file counts as empty.

    Read errors are raised so an unreadable target is never overwritten with merged content only.
    """
    if not path.exists():
        return b"", "utf-8", "\n", ParsedSettingsFile()
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    elif raw.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    else:
        try:
            raw.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            encoding = "latin-1"
    text = raw.decode(encoding, errors="ignore")
    crlf = text.count("\r\n")
    newline = "\r\n" if crlf >= text.count("\n") - crlf else "\n"
    return raw, encoding, newline, _parse_settings_text(text)


def _reconstruct_file_content(parsed: ParsedSettingsFile, newline: str) -> str:
    """Serializes all sections in order (including empty ones), each followed by a blank line."""
    by_section: Dict[str, List[Entry]] = {}
    for e in parsed.entries:
        by_section.setdefault(e.section.casefold(), []).append(e)
    lines: List[str] = []
    for sec in parsed.sections:
        lines.append(f"[{sec}]")
        lines.extend(f"{e.key}={e.value}" for e in by_section.get(sec.casefold(), []))
        lines.append("")
    return newline.join(lines)


def _extract_binding_info(key: str, value: str) -> Optional[Tuple[str, str]]:
    """Returns (device type, action) for input bindings like 'IK_E=(Action=Use)', else None."""
    m = re.search(r"Action\s*=\s*[\"']?([^,\"\')\s]+)", value, re.IGNORECASE)
    if not m:
        return None
    key_cf = key.casefold()
    dev_type = "controller" if "pad" in key_cf else ("ps4" if "ps4" in key_cf else "keyboard")
    return dev_type, m.group(1).casefold()


@functools.lru_cache(maxsize=4096)
def _get_identity(section: str, key: str, value: str, is_input: bool) -> Identity:
    """Identity used to match entries across files.

    In input.settings all keys bound to the same action and device form one group, so a mod
    rebinding an action replaces the old key instead of adding a second one. Everything else
    is matched by section and key.
    """
    sec_cf = section.casefold()
    if is_input:
        b_info = _extract_binding_info(key, value)
        if b_info:
            return ("binding", sec_cf, b_info[0], b_info[1])
    return ("option", sec_cf, key.casefold())


def _has_section(sections: List[str], name: str) -> bool:
    return name.casefold() in {s.casefold() for s in sections}


def _get_live_items(parsed: ParsedSettingsFile, ident: Identity, is_input: bool) -> Items:
    return [(e.key, e.value) for e in parsed.entries if _get_identity(e.section, e.key, e.value, is_input) == ident]


def _replace_group(parsed: ParsedSettingsFile, ident: Identity, section: str, items: Items, is_input: bool) -> None:
    """Replaces all entries of an identity with items at the position of the first old entry, adding the section if needed."""
    if not _has_section(parsed.sections, section):
        parsed.sections.append(section)
    matches = {i for i, e in enumerate(parsed.entries) if _get_identity(e.section, e.key, e.value, is_input) == ident}
    pos = min(matches, default=len(parsed.entries))
    kept = [e for i, e in enumerate(parsed.entries) if i not in matches]
    parsed.entries = kept[:pos] + [Entry(section, k, v) for k, v in items] + kept[pos:]


def _write_group(parsed: ParsedSettingsFile, created_sections: List[str], claim: Claim, is_input: bool) -> None:
    """Writes a claim; sections that did not exist before are remembered so rollback can remove them again."""
    if not _has_section(parsed.sections, claim.section) and not _has_section(created_sections, claim.section):
        created_sections.append(claim.section)
    _replace_group(parsed, claim.identity, claim.section, claim.items, is_input)


def _iter_fragments(fragment_dirs: List[Path]):
    """Yields (path, kind, source mod name) for every fragment, in the given directory order."""
    for fdir in fragment_dirs:
        if not fdir.is_dir():
            continue
        parent_name = fdir.parent.name
        source_label = "Overwrite" if parent_name.casefold() == "overwrite" else parent_name
        try:
            for p in sorted(fdir.iterdir(), key=lambda x: x.name.casefold()):
                kind = fragment_kind(p.name) if p.is_file() else None
                if kind:
                    yield p, kind, source_label
        except OSError:
            pass


def _collect_claims(fragment_dirs: List[Path], existing_targets: List[Path], log: Log) -> Dict[str, Dict[Identity, Claim]]:
    """Builds the winning claim per identity for each existing target file.

    fragment_dirs must be in ascending MO2 priority, so later mods override earlier ones.
    """
    target_names = {tf.name.lower() for tf in existing_targets}
    file_claims: Dict[str, Dict[Identity, Claim]] = {name: {} for name in target_names}

    for p, kind, source_label in _iter_fragments(fragment_dirs):
        try:
            entries = _load_parsed(p)[3].entries
        except OSError as e:
            log(f"Warning: Failed to read fragment '{p.name}' from '{source_label}', skipped: {e}", True)
            continue
        frag_path = str(p)
        if not entries:
            if frag_path not in _LOGGED_EMPTY_FRAGMENTS:
                _LOGGED_EMPTY_FRAGMENTS.add(frag_path)
                log(f"Fragment '{p.name}' from '{source_label}' contains no settings entries", False)
            continue
        _LOGGED_EMPTY_FRAGMENTS.discard(frag_path)

        is_input = kind == "input"
        grouped: Dict[Identity, List[Entry]] = {}
        for e in entries:
            grouped.setdefault(_get_identity(e.section, e.key, e.value, is_input), []).append(e)

        for tkey in (t for t in KIND_TARGETS[kind] if t in target_names):
            claims = file_claims[tkey]
            for ident, group in grouped.items():
                items = [(e.key, e.value) for e in group]
                prev = claims.get(ident)
                if prev and (prev.source != source_label or not _same(prev.items, items, ident[0] == "binding")):
                    log(f"Priority merge: Mod '{source_label}' overrides '{_describe_identity(ident)}' from '{prev.source}'", False)
                claims[ident] = Claim(ident, group[0].section, items, source_label, frag_path)

    return file_claims


def _rec_items(rec: Dict[str, Any], key: str) -> Items:
    """Items stored in a manifest record; 'absent' (entry did not exist before) yields []."""
    raw = rec.get(key)
    if not raw or raw == "absent":
        return []
    return [tuple(x) for x in raw]


def _classify(rec: Dict[str, Any], live_items: Items, is_bind: bool) -> str:
    """Returns 'ours', 'reset' or 'user' (see module docstring)."""
    if _same(live_items, _rec_items(rec, "written"), is_bind):
        return "ours"
    if not live_items or _same(live_items, _rec_items(rec, "baseline"), is_bind):
        return "reset"
    return "user"


def _rollback(parsed: ParsedSettingsFile, file_records: Dict[str, Any], claims: Dict[Identity, Claim], created_sections: List[str], is_input: bool, tf_name: str, log: Log) -> Tuple[bool, List[str]]:
    """Restores the baseline of recorded entries that no active mod claims anymore and drops their records.

    Entries changed by the user/game are kept. Returns (file changed, updated created_sections).
    """
    changed = False
    removed_counts: Dict[str, int] = {}

    for ident_key, rec in list(file_records.items()):
        ident = tuple(json.loads(ident_key))
        if ident in claims:
            continue
        state = _classify(rec, _get_live_items(parsed, ident, is_input), ident[0] == "binding")
        sec = rec.get("section", "")
        if state == "ours":
            _replace_group(parsed, ident, sec, _rec_items(rec, "baseline"), is_input)
            changed = True
            # Remove a section we created once it is empty again
            if _has_section(created_sections, sec) and not any(e.section.casefold() == sec.casefold() for e in parsed.entries):
                parsed.sections = [s for s in parsed.sections if s.casefold() != sec.casefold()]
                created_sections = [s for s in created_sections if s.casefold() != sec.casefold()]
            src = rec.get("source", "mod")
            removed_counts[src] = removed_counts.get(src, 0) + 1
        elif state == "user":
            log(f"Preserved changed entry '{_describe_identity(ident)}' (modified by user/game)", False)
        del file_records[ident_key]

    for src, count in removed_counts.items():
        log(f"Removed {count} {'entry' if count == 1 else 'entries'} in {tf_name} from '{src}' (mod no longer active)", False)

    return changed, created_sections


def _warn_key_reuse(base_entries: List[Entry], claims: Dict[Identity, Claim], claim: Claim, log: Log, warned: Set[frozenset]) -> None:
    """Warns when a newly bound key is already used for another action, in the base file or by another fragment."""
    action = claim.identity[3]
    sec_cf = claim.section.casefold()

    for k, _ in claim.items:
        k_cf = k.casefold()
        for base_e in base_entries:
            if base_e.section.casefold() == sec_cf and base_e.key.casefold() == k_cf:
                b_info = _extract_binding_info(base_e.key, base_e.value)
                if b_info and b_info[1] != action:
                    log(f"Key reuse warning: Key '{k}' bound to action '{action}' by '{claim.source}' in [{claim.section}] was previously bound to '{b_info[1]}'", True)

        for other_ident, other in claims.items():
            if other.frag_path == claim.frag_path or other.section.casefold() != sec_cf:
                continue
            other_action = other_ident[3] if len(other_ident) > 3 else ""
            if not other_action or other_action == action:
                continue
            if any(other_k.casefold() == k_cf for other_k, _ in other.items):
                pair = frozenset((claim.identity, other_ident, k_cf))
                if pair not in warned:
                    warned.add(pair)
                    log(f"Key reuse warning: Key '{k}' in [{claim.section}] is bound to both '{other_action}' and '{action}' ('{other.source}' and '{claim.source}')", True)


def _apply(parsed: ParsedSettingsFile, file_records: Dict[str, Any], claims: Dict[Identity, Claim], created_sections: List[str], is_input: bool, log: Log, base_entries: List[Entry]) -> Tuple[bool, List[str], int]:
    """Writes active claims and creates/updates their manifest records.

    Returns (file changed, updated created_sections, number of applied claims).
    """
    changed = False
    applied = 0
    warned: Set[frozenset] = set()

    for ident, claim in claims.items():
        ident_key = json.dumps(list(ident))
        live_items = _get_live_items(parsed, ident, is_input)
        is_bind = ident[0] == "binding"
        rec = file_records.get(ident_key)

        if rec:
            state = _classify(rec, live_items, is_bind)
            if state == "ours" and _same(claim.items, _rec_items(rec, "written"), is_bind):
                continue
            if state == "reset":
                log(f"Reset detected for '{_describe_identity(ident)}'; re-applying from '{claim.source}'", False)
            if state != "user":
                _write_group(parsed, created_sections, claim, is_input)
                changed = True
                applied += 1
                rec["written"] = [list(x) for x in claim.items]
            rec["source"] = claim.source
            continue

        if _same(live_items, claim.items, is_bind):
            continue
        if live_items:
            log(f"Replaced '{_format_items(live_items)}' in [{claim.section}] with '{_format_items(claim.items)}' from '{claim.source}'", False)
        if is_bind:
            _warn_key_reuse(base_entries, claims, claim, log, warned)
        file_records[ident_key] = {
            "section": claim.section,
            "written": [list(x) for x in claim.items],
            "baseline": [list(x) for x in live_items] if live_items else "absent",
            "source": claim.source,
        }
        _write_group(parsed, created_sections, claim, is_input)
        changed = True
        applied += 1

    return changed, created_sections, applied


def _scan_fragments(fragment_dirs: List[Path], target_dir: Path, log: Log) -> bool:
    """Logs fragment kinds whose target files do not exist yet; returns whether any fragment exists."""
    kinds_found = {kind for _, kind, _ in _iter_fragments(fragment_dirs)}
    for kind, targets in KIND_TARGETS.items():
        if kind in kinds_found and not any((target_dir / t).exists() for t in targets):
            log(f"{targets[0]} not found; skipped fragments (start the game once to create it)", False)
    return bool(kinds_found)


def _load_manifest(path: Path, target_dir: Path, has_frags: bool, log: Log) -> Dict[str, Any]:
    """Loads the manifest; a corrupt one is treated as empty."""
    manifest: Dict[str, Any] = {}
    corrupt = False
    if path.exists():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"Warning: Corrupt manifest {path.name}, resetting: {e}", True)
            corrupt = True

    # Backups without a manifest mean an earlier merge happened but its records are lost
    has_backup = target_dir.exists() and any(target_dir.glob("*" + BACKUP_SUFFIX))
    if has_frags and (corrupt or (not path.exists() and has_backup)):
        log("Manifest missing or corrupt; previously merged entries can no longer be rolled back.", False)
    return manifest


def _save_manifest(path: Path, manifest: Dict[str, Any], manifest_records: Dict[str, Any], log: Log, remove_backups: bool) -> None:
    """Writes the manifest, or deletes it when empty (plus the backups if everything was rolled back).

    Write errors are raised so the caller can restore the target files.
    """
    if manifest_records:
        manifest["records"] = manifest_records
        _atomic_write(path, json.dumps(manifest, indent=2).encode("utf-8"))
        return

    if path.exists():
        try:
            path.unlink()
        except OSError as e:
            log(f"Warning: Failed to remove empty manifest: {e}", True)
    if remove_backups and path.parent.exists():
        for backup_file in path.parent.glob("*" + BACKUP_SUFFIX):
            try:
                backup_file.unlink()
            except OSError as e:
                log(f"Warning: Failed to remove backup file {backup_file.name}: {e}", True)


def _write_target(tf: Path, parsed: ParsedSettingsFile, raw: bytes, encoding: str, newline: str, log: Log) -> bool:
    """Saves the pre-merge content as backup, then writes the merged file in its original encoding.

    Raises if the backup cannot be written, so no file is changed without one.
    Returns whether the backup was newly created.
    """
    backup_path = tf.with_suffix(tf.suffix + BACKUP_SUFFIX)
    existed = backup_path.exists()
    try:
        backup_path.write_bytes(raw)
    except Exception as e:
        raise RuntimeError(f"Failed to create backup for {tf.name}, aborting merge: {e}") from e
    if not existed:
        log(f"Created backup for {tf.name} at {backup_path.name}", False)
    _atomic_write(tf, _reconstruct_file_content(parsed, newline).encode(encoding))
    return not existed


def merge_settings(fragment_dirs: List[Path], target_dir: Path, log: Log) -> None:
    """Merges fragments into the settings files in target_dir and rolls back entries of inactive mods.

    fragment_dirs must be ordered by ascending MO2 priority; an empty list rolls back everything.
    If anything fails while writing, all already modified files are restored.
    """
    raw_log = log

    def log(msg: str, warn: bool = False) -> None:
        # A failing log call must never abort the merge and trigger a rollback
        try:
            raw_log(msg, warn)
        except Exception:
            pass

    has_frags = _scan_fragments(fragment_dirs, target_dir, log)
    if not target_dir.exists():
        return
    existing_targets = [target_dir / t for targets in KIND_TARGETS.values() for t in targets if (target_dir / t).exists()]
    if not existing_targets:
        return

    manifest_path = target_dir / MANIFEST_FILENAME
    manifest = _load_manifest(manifest_path, target_dir, has_frags, log)
    loaded = {tf: _load_parsed(tf) for tf in existing_targets}
    file_claims = _collect_claims(fragment_dirs, existing_targets, log)
    manifest_records: Dict[str, Any] = manifest.get("records", {})
    had_records = bool(manifest_records)
    modified_files: List[Path] = []
    new_backups: List[Path] = []

    try:
        for tf in existing_targets:
            tf_name = tf.name.lower()
            claims = file_claims.get(tf_name, {})
            raw, encoding, newline, parsed = loaded[tf]
            is_input = tf_name == "input.settings"
            target_data = manifest_records.get(tf_name, {})
            file_records: Dict[str, Any] = target_data.get("records", {})
            created_sections: List[str] = target_data.get("created_sections", [])

            changed_rb, created_sections = _rollback(parsed, file_records, claims, created_sections, is_input, tf.name, log)
            # Entries no claim touches; used to detect key conflicts with the user's own bindings
            base_entries = [e for e in parsed.entries if _get_identity(e.section, e.key, e.value, is_input) not in claims]
            changed_ap, created_sections, applied = _apply(parsed, file_records, claims, created_sections, is_input, log, base_entries)

            if changed_rb or changed_ap:
                if _write_target(tf, parsed, raw, encoding, newline, log):
                    new_backups.append(tf.with_suffix(tf.suffix + BACKUP_SUFFIX))
                modified_files.append(tf)
            if applied:
                log(f"Applied {applied} entries in {tf.name}", False)

            if file_records:
                manifest_records[tf_name] = {"created_sections": created_sections, "records": file_records}
            else:
                manifest_records.pop(tf_name, None)

        # Records of target files that no longer exist cannot be rolled back anymore
        existing_names = {tf.name.lower() for tf in existing_targets}
        for orphan in [n for n in manifest_records if n not in existing_names]:
            log(f"Dropped manifest records for missing file {orphan}", False)
            del manifest_records[orphan]

        _save_manifest(manifest_path, manifest, manifest_records, log, remove_backups=had_records)

    except Exception as e:
        log(f"Error during settings merge: {e}", True)
        for tf in modified_files:
            try:
                _atomic_write(tf, loaded[tf][0])
            except Exception as rb_e:
                log(f"Failed to rollback {tf.name}: {rb_e}", True)
        # Backups created by this failed run would later be mistaken for a lost manifest
        for backup in new_backups:
            backup.unlink(missing_ok=True)
