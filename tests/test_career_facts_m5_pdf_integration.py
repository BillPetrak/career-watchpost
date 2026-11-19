from __future__ import annotations

import hashlib
from pathlib import Path

from careeros.career_profile.workspace import analyze, candidate_detail, candidates
from careeros.database.connection import connect
from careeros.database.resume_intelligence import validate_recognized_fact_authority
from careeros.database.schema import initialize_schema
from careeros.materials.service import create_material
from careeros.materials.storage import stage_bytes
from careeros.resume_intelligence.career_facts import (
    DETERMINISTIC_EXTRACTOR_FINGERPRINT,
)


def positioned_pdf(pages: list[list[str]]) -> bytes:
    streams = []
    for lines in pages:
        commands = [b"BT /F1 11 Tf 24 280 Td"]
        for index, line in enumerate(lines):
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            if index:
                commands.append(b"0 -16 Td")
            commands.append(f"({escaped}) Tj".encode("cp1252"))
        streams.append(b" ".join(commands) + b" ET")
    page_ids = [4 + index * 2 for index in range(len(streams))]
    kids = b"[" + b" ".join(f"{page_id} 0 R".encode() for page_id in page_ids) + b"]"
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Count " + str(len(streams)).encode() + b"/Kids" + kids + b">>",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica/Encoding/WinAnsiEncoding>>",
    ]
    for stream in streams:
        page_id = len(objects) + 1
        stream_id = page_id + 1
        objects.extend(
            [
                b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 400 400]/"
                b"Resources<</Font<</F1 3 0 R>>>>/Contents "
                + str(stream_id).encode()
                + b" 0 R>>",
                b"<</Length "
                + str(len(stream)).encode()
                + b">>\nstream\n"
                + stream
                + b"\nendstream",
            ]
        )
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    output.extend(
        b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    )
    trailer = (
        f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n"
    )
    output.extend(trailer.encode())
    return bytes(output)


def two_column_pdf() -> bytes:
    """Build a positioned, fictional PDF with independent visual columns."""
    left = [
        (24, 280, "Résumé"),
        (24, 264, "SKILLS"),
        (24, 248, "o SQL Database"),
        (40, 232, "  Management"),
        (24, 216, "o Linux/Unix Command"),
        (40, 200, "  line"),
        (24, 184, "o Python"),
        (24, 168, "o C++"),
        (24, 152, "o JAVA"),
    ]
    right = [
        (220, 280, "LANGUAGES"),
        (220, 264, "o English:"),
        (236, 248, "  Proficient"),
        (220, 232, "o Hindi: Proficient"),
    ]
    commands = [b"BT /F1 11 Tf"]
    for x, y, line in [*left, *right]:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        commands.append(f"1 0 0 1 {x} {y} Tm ({escaped}) Tj".encode("cp1252"))
    stream = b" ".join(commands) + b" ET"
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Count 1/Kids[4 0 R]>>",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica/Encoding/WinAnsiEncoding>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 400 400]/"
        b"Resources<</Font<</F1 3 0 R>>>>/Contents 5 0 R>>",
        b"<</Length "
        + str(len(stream)).encode()
        + b">>\nstream\n"
        + stream
        + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    output.extend(
        b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    )
    output.extend(
        f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R>>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)


def test_two_column_pdf_assembles_v3_wrapped_skills(tmp_path: Path) -> None:
    db = connect(tmp_path / "two-column.sqlite3")
    initialize_schema(db)
    root = tmp_path / "materials"
    content = two_column_pdf()
    stage, size, sha = stage_bytes(root, [content])
    detail = create_material(
        db,
        root,
        name="Synthetic two-column résumé",
        category="resume",
        filename="two-column.pdf",
        media_type="application/pdf",
        stage=stage,
        size=size,
        sha256=sha,
    )
    result = analyze(
        db, root, detail["material_id"], detail["current_version"]["version_id"]
    )
    assert result["outcome"] == "succeeded"
    authority = validate_recognized_fact_authority(db, result["fact_run_id"])
    assert authority.run.extractor_fingerprint == DETERMINISTIC_EXTRACTOR_FINGERPRINT
    assert (
        db.execute(
            "SELECT count(*) FROM resume_text_segments WHERE extraction_run_id=?",
            (result["extraction_run_id"],),
        ).fetchone()[0]
        == 1
    )
    page = candidates(db, result["fact_run_id"])
    skills = {
        item["value"]["name"] for item in page["items"] if item["fact_type"] == "skill"
    }
    assert skills == {
        "SQL Database Management",
        "Linux/Unix Command line",
        "Python",
        "C++",
        "JAVA",
    }
    assert {
        item["value"]["name"]
        for item in page["items"]
        if item["fact_type"] == "language"
    } == {"English", "Hindi"}
    raw = (
        db.execute(
            "SELECT raw_text FROM resume_extraction_runs WHERE id=?",
            (result["extraction_run_id"],),
        )
        .fetchone()[0]
        .encode()
    )
    expected_fragments = {
        "SQL Database Management": ["SQL Database", "Management"],
        "Linux/Unix Command line": ["Linux/Unix Command", "line"],
    }
    for item in page["items"]:
        name = item["value"].get("name")
        if name not in expected_fragments:
            continue
        detail_view = candidate_detail(db, item["id"])
        evidence = detail_view["evidence"]["items"]
        assert len(evidence) == 2
        assert [e["ordinal"] for e in evidence] == [0, 1]
        assert [
            raw[e["start_byte"] : e["end_byte"]].decode() for e in evidence
        ] == expected_fragments[name]
        assert all(e["source_label"] == "PDF page 1" for e in evidence)
        stored_hashes = [
            row["slice_sha256"]
            for row in db.execute(
                "SELECT slice_sha256 FROM career_fact_evidence "
                "WHERE candidate_id=? ORDER BY ordinal",
                (item["id"],),
            ).fetchall()
        ]
        assert stored_hashes == [
            hashlib.sha256(raw[e["start_byte"] : e["end_byte"]]).hexdigest()
            for e in evidence
        ]
        assert evidence[0]["start_byte"] > len("Résumé".encode())
    db.close()


def test_positioned_pdf_runs_m3_m5_authority_and_m7_projection(tmp_path: Path) -> None:
    db = connect(tmp_path / "integration.sqlite3")
    initialize_schema(db)
    root = tmp_path / "materials"
    content = positioned_pdf(
        [
            [
                "Contact Details",
                "Name: Zoë Fictional",
                "Address: 42 Fictional Street, Athens",
                "Email: owner@example.test",
                "Phone: +30 210 123 4567",
                "Summary",
                "A sentence that is not a skill.",
                "Skills:",
                "o Python",
                "  o OpenGL",
                "  o Objective-C",
                "  o Oracle",
                "  o OAuth",
                "  o C",
                "  o C++",
                "  o C#",
                "  o SQL Database",
                "  owner@example.test",
                "  +30 210 123 4567",
                "Projects",
                "  o Fictional API",
                "Professional Experience",
                "- Senior Engineer at Fictional Labs — 2020-01 - 2022",
                "Languages",
                "o English:",
                "      Proficient",
                "  o Hindi: Proficient",
                "  - English",
                "  - English: Fluent",
                "  - Hindi - Advanced",
                "  English, Hindi: Proficient",
                "  English: C2",
                "  Hindi: B1",
                "  English: LRN Proficiency C2 (Distinction)",
                "  Hindi: Goethe-Zertifikat B1 (Befriedigend)",
                "  English:",
                "    Fluent",
                "  Proficient",
                "  o CrossPage:",
            ],
            ["Proficient", "References", "  o Fictional Contact"],
        ]
    )
    stage, size, sha = stage_bytes(root, [content])
    detail = create_material(
        db,
        root,
        name="Synthetic positioned résumé",
        category="resume",
        filename="synthetic.pdf",
        media_type="application/pdf",
        stage=stage,
        size=size,
        sha256=sha,
    )
    result = analyze(
        db, root, detail["material_id"], detail["current_version"]["version_id"]
    )
    assert result["outcome"] == "succeeded"
    authority = validate_recognized_fact_authority(db, result["fact_run_id"])
    assert authority.run.id == result["fact_run_id"]
    assert (
        db.execute(
            "SELECT count(*) FROM resume_text_segments WHERE extraction_run_id=?",
            (result["extraction_run_id"],),
        ).fetchone()[0]
        == 2
    )
    segments = db.execute(
        "SELECT raw_start_byte,raw_end_byte,source_locator FROM resume_text_segments "
        "WHERE extraction_run_id=? ORDER BY ordinal",
        (result["extraction_run_id"],),
    ).fetchall()
    assert [segment["source_locator"] for segment in segments] == [
        '{"page":1,"type":"pdf_page"}',
        '{"page":2,"type":"pdf_page"}',
    ]
    page = candidates(db, result["fact_run_id"])
    assert any(
        item["fact_type"] == "identity.name"
        and item["value"] == {"name": "Zoë Fictional"}
        for item in page["items"]
    )
    assert any(
        item["fact_type"] == "contact.email"
        and item["value"] == {"email": "owner@example.test"}
        for item in page["items"]
    )
    assert any(
        item["fact_type"] == "contact.phone"
        and item["value"] == {"phone": "+30 210 123 4567"}
        for item in page["items"]
    )
    language_items = {
        item["value"]["name"]
        for item in page["items"]
        if item["fact_type"] == "language"
    }
    assert language_items == {"English", "Hindi"}
    assert "Proficient" not in language_items
    skill_items = {
        item["value"]["name"] for item in page["items"] if item["fact_type"] == "skill"
    }
    assert {
        "Python",
        "OpenGL",
        "Objective-C",
        "Oracle",
        "OAuth",
        "C",
        "C++",
        "C#",
        "SQL Database",
    }.issubset(skill_items)
    assert not {"owner@example.test", "+30 210 123 4567"} & skill_items
    assert not {
        "42 Fictional Street, Athens",
        "A sentence that is not a skill.",
        "Fictional API",
        "Fictional Contact",
        "CrossPage",
        "Proficient",
    } & (skill_items | language_items)
    employment = next(
        item for item in page["items"] if item["fact_type"] == "employment"
    )
    assert employment["value"]["title"] == "Senior Engineer"
    assert "Senior Engineer" not in skill_items
    english = next(
        item for item in page["items"] if item["value"] == {"name": "English"}
    )
    hindi = next(item for item in page["items"] if item["value"] == {"name": "Hindi"})
    raw = (
        db.execute(
            "SELECT raw_text FROM resume_extraction_runs WHERE id=?",
            (result["extraction_run_id"],),
        )
        .fetchone()[0]
        .encode()
    )
    zoe_end = raw.index("Zoë".encode()) + len("Zoë".encode())
    for candidate, label in ((english, "English"), (hindi, "Hindi")):
        detail_view = candidate_detail(db, candidate["id"])
        assert detail_view["evidence"]["items"]
        assert all(
            evidence["source_label"] == "PDF page 1"
            for evidence in detail_view["evidence"]["items"]
        )
        for evidence in detail_view["evidence"]["items"]:
            start, end = evidence["start_byte"], evidence["end_byte"]
            assert start > zoe_end
            assert raw[start:end].decode() == label
            stored_hash = db.execute(
                "SELECT slice_sha256 FROM career_fact_evidence WHERE candidate_id=? "
                "AND start_byte=? AND end_byte=?",
                (candidate["id"], start, end),
            ).fetchone()[0]
            assert stored_hash == hashlib.sha256(raw[start:end]).hexdigest()
    db.close()
