"""Inspect the vendor-imported catalog: how many are real-named vs auto-named."""
from autocad_mcp.library.catalog import AssetCatalog, default_library_root

cat = AssetCatalog(default_library_root())

total_vendor = cat.conn.execute(
    "SELECT COUNT(*) FROM assets WHERE id LIKE 'vendor/%'"
).fetchone()[0]

auto = cat.conn.execute(
    r"""SELECT COUNT(*) FROM assets
        WHERE id LIKE 'vendor/%'
          AND block_name LIKE 'A$%'"""
).fetchone()[0]

print(f"vendor total: {total_vendor}")
print(f"auto-named (A$*): {auto}  ({100*auto/total_vendor:.1f}%)")
print(f"real-named: {total_vendor - auto}")

print("\nSample real-named sofas/seating:")
rows = cat.conn.execute(
    r"""SELECT id, block_name FROM assets
        WHERE id LIKE 'vendor/%' AND category = 'furniture/seating'
          AND block_name NOT LIKE 'A$%'
        LIMIT 10"""
).fetchall()
for r in rows:
    print(f"  block_name={r['block_name']!r:60s}")

print("\nSample real-named beds:")
rows = cat.conn.execute(
    r"""SELECT id, block_name FROM assets
        WHERE id LIKE 'vendor/%' AND category = 'furniture/beds'
          AND block_name NOT LIKE 'A$%'
        LIMIT 10"""
).fetchall()
for r in rows:
    print(f"  block_name={r['block_name']!r:60s}")
