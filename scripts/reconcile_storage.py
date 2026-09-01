#!/usr/bin/env python
"""
Level 8: Cross-Storage Data Consistency Reconciliation CLI
Audits and repairs data drift between PostgreSQL, MinIO, Qdrant, and Memgraph.

Usage:
    python scripts/reconcile_storage.py --dry-run
    python scripts/reconcile_storage.py --fix
    python scripts/reconcile_storage.py --tenant tenant_alpha --fix
"""
import sys
import os
import argparse
import asyncio

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.storage.reconciliation import StorageReconciler


async def main():
    parser = argparse.ArgumentParser(description="Cross-Storage Consistency Auditor & Reconciler")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Scan and report drifts without modifying data (Default)")
    parser.add_argument("--fix", action="store_true", help="Execute safe repair and cleanup of orphan records")
    parser.add_argument("--tenant", type=str, default=None, help="Filter reconciliation scope to a specific tenant ID")

    args = parser.parse_args()
    is_dry_run = not args.fix  # If --fix is specified, dry_run is False

    print("=" * 70)
    print(" 🛠️  CROSS-STORAGE CONSISTENCY AUDITOR & RECONCILER (Level 8)")
    print("=" * 70)
    print(f" Mode: {'🔍 AUDIT ONLY (DRY-RUN)' if is_dry_run else '⚡ SAFE REPAIR & CLEANUP (--fix)'}")
    print(f" Tenant Scope: {args.tenant or 'ALL TENANTS (Global)'}")
    print(" Scanning storage tiers: PostgreSQL (SSOT), MinIO (SSOT), Qdrant (Vectors), Memgraph (Graph)...")
    print("-" * 70)

    # 1. Audit Consistency
    report = await StorageReconciler.audit_consistency(tenant_id=args.tenant)

    print("\n📊 Storage Inventory Summary:")
    print(f"  • PostgreSQL Documents (SSOT): {report.pg_docs_count}")
    print(f"  • MinIO Raw Objects (SSOT):    {report.minio_objects_count}")
    print(f"  • Qdrant Vector Chunks:        {report.qdrant_points_count}")
    print(f"  • Memgraph Knowledge Edges:    {report.memgraph_edges_count}")

    print("\n🔍 Inconsistency & Drift Audit Results:")
    if not report.has_drift:
        print("  ✅ Zero Drift Detected! All storage tiers are 100% consistent.")
    else:
        print(f"  ⚠️  Detected {len(report.drifts)} data drift items:")
        for idx, d in enumerate(report.drifts):
            print(f"    [{idx+1}] Tier: {d.storage.upper():<10} | Type: {d.drift_type:<20} | Doc ID: {d.doc_id} | Tenant: {d.tenant_id}")
            if d.details:
                print(f"        Details: {d.details}")

    # 2. Repair if --fix
    if args.fix and report.has_drift:
        print("\n⚡ Executing Safe Cleanup & Repair...")
        result = await StorageReconciler.repair_consistency(report, dry_run=False)
        print("  ✓ Cleanup Results:")
        print(f"    - Orphan Vectors Removed:   {result.cleaned_orphan_vectors}")
        print(f"    - Orphan Graph Edges Purged: {result.cleaned_orphan_edges}")
        print(f"    - Orphan Files Removed:     {result.cleaned_orphan_files}")
        print(f"    - Duration:                 {result.duration_seconds}s")
        print("\n🎉 Storage reconciliation successfully completed!")
    elif is_dry_run and report.has_drift:
        print("\n💡 Run with '--fix' to safely remove detected orphan records.")

    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
