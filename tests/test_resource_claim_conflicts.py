"""Discriminantes de Fase 4: ResourceClaims y detección pura de conflictos."""

from __future__ import annotations

import random
import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from punto.project.resource_claims import (
    ClaimOrigin,
    ConflictClass,
    ConflictStatus,
    InvalidResourceClaimError,
    ResourceClaim,
    claims_from_scheduling,
    detect_conflicts,
)
from punto.project.resources import ResourceDimension, ResourceSet
from punto.schemas.scheduling import (
    ResourceAccess,
    ResourceReference,
    TaskSchedulingRecord,
)

TASK_A = UUID("10000000-0000-0000-0000-000000000001")
TASK_B = UUID("20000000-0000-0000-0000-000000000002")


def _claim(
    task_id: UUID,
    resource_type: str,
    key: str,
    access: str,
    *,
    origin: str = "PLAN",
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "resource_type": resource_type,
        "resource_key": key,
        "access_mode": access,
        "origin": origin,
    }


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


def test_a_read_read_same_file_is_compatible() -> None:
    report = detect_conflicts(
        [_claim(TASK_A, "file", "src/a.ts", "READ")],
        [_claim(TASK_B, "file", "src/a.ts", "READ")],
    )

    assert report.status is ConflictStatus.COMPATIBLE
    assert report.conflicts == ()


def test_b_read_write_same_file_conflicts() -> None:
    report = detect_conflicts(
        [_claim(TASK_A, "file", "src/a.ts", "READ")],
        [_claim(TASK_B, "file", "src/a.ts", "WRITE")],
    )

    assert report.status is ConflictStatus.CONFLICT
    assert report.conflicts[0].conflict_class is ConflictClass.PATH_OVERLAP
    assert report.conflicts[0].resource_key == "file:src/a.ts"


def test_c_write_write_same_file_conflicts() -> None:
    report = detect_conflicts(
        [_claim(TASK_A, "file", "src/a.ts", "WRITE")],
        [_claim(TASK_B, "file", "src/a.ts", "WRITE")],
    )

    assert report.status is ConflictStatus.CONFLICT


def test_d_different_files_are_compatible() -> None:
    report = detect_conflicts(
        [_claim(TASK_A, "file", "src/a.ts", "WRITE")],
        [_claim(TASK_B, "file", "src/b.ts", "WRITE")],
    )

    assert report.status is ConflictStatus.COMPATIBLE


def test_e_path_prefix_conflicts_with_descendant_file() -> None:
    report = detect_conflicts(
        [_claim(TASK_A, "path", "src/auth/**", "WRITE")],
        [_claim(TASK_B, "file", "src/auth/session.ts", "READ")],
    )

    assert report.status is ConflictStatus.CONFLICT
    assert report.conflicts[0].resource_key == "file:src/auth/session.ts"


def test_f_disjoint_path_and_file_are_compatible() -> None:
    report = detect_conflicts(
        [_claim(TASK_A, "path", "src/auth/**", "WRITE")],
        [_claim(TASK_B, "file", "src/catalog/item.ts", "READ")],
    )

    assert report.status is ConflictStatus.COMPATIBLE


def test_g_path_boundaries_do_not_use_naive_prefixes() -> None:
    report = detect_conflicts(
        [_claim(TASK_A, "path", "src/auth", "WRITE")],
        [_claim(TASK_B, "file", "src/authentication/x.ts", "READ")],
    )

    assert report.status is ConflictStatus.COMPATIBLE


def test_path_prefixes_overlap_only_on_component_boundaries() -> None:
    overlapping = detect_conflicts(
        [_claim(TASK_A, "path", "src/**", "WRITE")],
        [_claim(TASK_B, "path", "src/catalog/**", "READ")],
    )
    disjoint = detect_conflicts(
        [_claim(TASK_A, "path", "src/auth/**", "WRITE")],
        [_claim(TASK_B, "path", "src/authentication/**", "READ")],
    )

    assert overlapping.status is ConflictStatus.CONFLICT
    assert overlapping.conflicts[0].resource_key == "path:src/catalog"
    assert disjoint.status is ConflictStatus.COMPATIBLE


def test_h_same_contract_claim_conflicts() -> None:
    report = detect_conflicts(
        [_claim(TASK_A, "contract", "Property", "WRITE")],
        [_claim(TASK_B, "contract", "property", "READ")],
    )

    assert report.status is ConflictStatus.CONFLICT
    assert report.conflicts[0].conflict_class is ConflictClass.LOGICAL_RESOURCE
    assert report.conflicts[0].resource_key == "contract:property"


def test_i_different_logical_resources_are_compatible() -> None:
    report = detect_conflicts(
        [_claim(TASK_A, "contract", "Property", "WRITE")],
        [_claim(TASK_B, "contract", "User", "READ")],
    )

    assert report.status is ConflictStatus.COMPATIBLE


@pytest.mark.parametrize("resource_type", ["schema", "api", "interface", "database_table"])
def test_minimum_logical_namespaces_conflict_only_on_explicit_same_key(
    resource_type: str,
) -> None:
    key = "/properties" if resource_type == "api" else "Property"
    report = detect_conflicts(
        [_claim(TASK_A, resource_type, key, "WRITE")],
        [_claim(TASK_B, resource_type, key, "READ")],
    )

    assert report.status is ConflictStatus.CONFLICT
    assert report.conflicts[0].conflict_class is ConflictClass.LOGICAL_RESOURCE


def test_j_same_explicit_functional_chain_conflicts() -> None:
    report = detect_conflicts(
        [_claim(TASK_A, "chain", "property-types", "WRITE")],
        [_claim(TASK_B, "chain", "property-types", "READ")],
    )

    assert report.status is ConflictStatus.CONFLICT
    assert report.conflicts[0].conflict_class is ConflictClass.FUNCTIONAL_CHAIN


def test_k_different_real_worktrees_do_not_hide_logical_conflict(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "README.md").write_text("base\n", encoding="utf-8")
    _git(target, "init", "-b", "main")
    _git(target, "add", "README.md")
    _git(
        target,
        "-c",
        "user.name=PUNTO Fixture",
        "-c",
        "user.email=fixture@punto.local",
        "commit",
        "-m",
        "base",
    )
    worktree_a = tmp_path / "worktree-a"
    worktree_b = tmp_path / "worktree-b"
    _git(target, "worktree", "add", "-b", "task-a", str(worktree_a), "HEAD")
    _git(target, "worktree", "add", "-b", "task-b", str(worktree_b), "HEAD")

    report = detect_conflicts(
        [_claim(TASK_A, "contract", "Property", "WRITE")],
        [_claim(TASK_B, "contract", "Property", "READ")],
    )

    assert worktree_a.resolve() != worktree_b.resolve()
    assert report.status is ConflictStatus.CONFLICT


def test_l_equivalent_path_formats_normalize_to_one_resource() -> None:
    report = detect_conflicts(
        [_claim(TASK_A, "file", ".\\src\\Auth\\session.ts", "WRITE")],
        [_claim(TASK_B, "file", "src/auth/session.ts", "READ")],
    )

    assert report.status is ConflictStatus.CONFLICT
    assert {claim.resource_key for claim in report.normalized_claims} == {
        "src/auth/session.ts"
    }


def test_m_path_traversal_is_invalid_and_fails_closed() -> None:
    with pytest.raises(InvalidResourceClaimError) as captured:
        detect_conflicts(
            [_claim(TASK_A, "file", "../secret", "WRITE")],
            [_claim(TASK_B, "file", "secret", "READ")],
        )

    assert captured.value.code == "INVALID_RESOURCE_CLAIM"
    assert "traversal" in captured.value.detail


def test_n_unknown_access_mode_is_invalid_and_fails_closed() -> None:
    with pytest.raises(InvalidResourceClaimError) as captured:
        detect_conflicts(
            [_claim(TASK_A, "file", "src/a.ts", "EXCLUSIVE")],
            [_claim(TASK_B, "file", "src/a.ts", "READ")],
        )

    assert captured.value.code == "INVALID_RESOURCE_CLAIM"
    assert "access_mode desconocido" in captured.value.detail


def test_o_duplicate_claims_produce_one_stable_conflict() -> None:
    duplicates = [
        _claim(TASK_A, "file", "src/a.ts", "WRITE", origin="PLAN"),
        _claim(TASK_A, "file", "./src/a.ts", "WRITE", origin="HUMAN"),
    ]
    report = detect_conflicts(
        duplicates,
        [_claim(TASK_B, "file", "src/a.ts", "READ")],
    )

    assert len(report.normalized_claims) == 2
    assert len(report.conflicts) == 1
    assert report.normalized_claims[0].origin is ClaimOrigin.HUMAN


def test_p_swapping_task_groups_produces_identical_report() -> None:
    left = [_claim(TASK_A, "path", "src/**", "WRITE")]
    right = [_claim(TASK_B, "file", "src/catalog/a.ts", "READ")]

    assert detect_conflicts(left, right) == detect_conflicts(right, left)


def test_q_random_claim_order_produces_identical_report() -> None:
    left = [
        _claim(TASK_A, "file", "src/a.ts", "WRITE"),
        _claim(TASK_A, "contract", "Property", "READ"),
        _claim(TASK_A, "chain", "publication", "READ"),
    ]
    right = [
        _claim(TASK_B, "file", "src/a.ts", "READ"),
        _claim(TASK_B, "contract", "Property", "WRITE"),
        _claim(TASK_B, "chain", "publication", "WRITE"),
    ]
    expected = detect_conflicts(left, right)
    random.Random(42).shuffle(left)
    random.Random(84).shuffle(right)

    assert detect_conflicts(left, right) == expected


@pytest.mark.parametrize("left,right", [([], []), ([], [_claim(TASK_B, "file", "a", "READ")])])
def test_r_absent_claims_are_explicitly_insufficient(
    left: list[dict[str, object]], right: list[dict[str, object]]
) -> None:
    report = detect_conflicts(left, right)

    assert report.status is ConflictStatus.INSUFFICIENT_CLAIMS
    assert report.conflicts == ()


def test_scheduling_v2_adapter_and_existing_resource_set_need_no_schema_migration() -> None:
    record = TaskSchedulingRecord(
        managed=True,
        resources=(
            ResourceReference(kind="path", key="src/auth/**", access=ResourceAccess.WRITE),
        ),
    )

    claims = claims_from_scheduling(TASK_A, record)
    resources = ResourceSet.from_claims(claims)

    assert claims == (
        ResourceClaim(
            task_id=TASK_A,
            resource_type=ResourceDimension.PATH,
            resource_key="src/auth",
            access_mode=ResourceAccess.WRITE,
            origin=ClaimOrigin.TASK_DEFINITION,
        ),
    )
    assert resources.tokens == ("path:src/auth",)


@pytest.mark.parametrize(
    "bad_claim",
    [
        _claim(TASK_A, "unknown", "src/a", "WRITE"),
        _claim(TASK_A, "contract", " ", "WRITE"),
        _claim(TASK_A, "path", "src/*/a", "WRITE"),
        _claim(TASK_A, "file", "C:/repo/a", "WRITE"),
    ],
)
def test_invalid_resource_types_keys_globs_and_absolute_paths_fail_closed(
    bad_claim: dict[str, object],
) -> None:
    with pytest.raises(InvalidResourceClaimError):
        detect_conflicts([bad_claim], [_claim(TASK_B, "file", "src/a", "READ")])


def test_logical_namespaces_do_not_invent_heuristic_conflicts() -> None:
    report = detect_conflicts(
        [_claim(TASK_A, "schema", "Listing", "WRITE")],
        [_claim(TASK_B, "api", "/listings", "READ")],
    )

    assert report.status is ConflictStatus.COMPATIBLE
