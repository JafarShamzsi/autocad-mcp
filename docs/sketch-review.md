# Floor Plan Review — Modern House (120.4 m²)

Date: 2026-05-17
Source: model-space screenshot of a Claude-driven autocad-mcp drafting session

## TL;DR

The plan reads as architectural at a glance but falls apart on inspection. Two failures dominate everything else:

1. **Almost no native AutoCAD entities are used.** Text labels are drawn as line segments forming letter shapes, walls are stacks of small rectangles instead of polylines or double-line walls, fixtures and furniture are hand-built from circles and lines, the title block is line-geometry "typography". The drawing is unparseable to AutoCAD's own tools — none of it is searchable, dimensionable from object data, schedulable, or selectable as semantic objects.
2. **No asset library is being used.** Every chair, toilet, sink, bed, table, window, and door swing was reconstructed from primitives. The result varies turn-to-turn and looks crude up close.

These two problems are not Claude being lazy. They are the system not giving Claude the right tools at the right moment with the right defaults. Fix the system and the output corrects itself.

## What I can see in the screenshot

| Element | Observation | Verdict |
|---|---|---|
| Title bar / room labels | "MODERN HOUSE — FLOOR PLAN", "MASTER BEDROOM", "DINING", "KITCHEN", area tags "0 13 m²" | Drawn as line strokes forming letters — not TEXT or MTEXT entities |
| Exterior walls | Cyan, made of many short rectangles repeated along the perimeter | Probably Claude's attempt at "window blocks" stamped into a wall; not a continuous double-line wall |
| Interior walls | Mixed — some look like single lines, some like rectangle patches | No consistent wall primitive, no cleanup at intersections |
| Doors | Arcs visible at openings — that part is correct | Door arcs OK, but no door block; the leaf is just a line |
| Windows | Small clustered rectangles on the exterior | Decorative; not a window block, no glazing line |
| Hatch fills (dining rug, kitchen counter, bed, master bath) | Magenta, blocky patterns | Probably ad-hoc hatching, not from a named pattern (ANSI31 / AR-BRICK / AR-CONC) |
| Furniture (dining table + chairs, sofa, beds) | Composed from circles, rectangles, and hatched shapes | No furniture blocks from a library |
| Plumbing fixtures (toilet, sinks, tub) | Approximate U-shapes and rectangles | No fixture blocks; sizes inconsistent |
| Kitchen appliances | Four magenta circles for the cooktop burners, plus rectangles for cabinets | Crude but identifiable; not block-based |
| Dimensions | None visible — only a scale bar at bottom-left | The drafting workflow stopped before the dimension step |
| North arrow | A circle with "N" mark on the right | Drawn from primitives; should be a standard NORTH block |
| Title block | A row of text-lines and a scale bar at the bottom | Drawn as line-letters; should be a TBLK_* block with filled attributes |
| Layer use | Multiple colors imply some layer discipline, but I can't see the layer panel | Possibly NCS-shaped — worth auditing |
| Annotation scale | Text heights vary | No annotative styles in use |

The plan was drawn against the `file_ipc` backend with real AutoCAD (the dark UI is AutoCAD's model space). So nothing here is a backend capability limit — every fix below is reachable today.

## Diagnosis: why each failure happens

### 1. Text rendered as line geometry

**Smoking gun.** "0 13 m²" labels and room names are sequences of short line segments. AutoCAD has TEXT, MTEXT, and ATTRIB entities; Claude used LINE.

**Why this happens.**
- `dim-annotate/SKILL.md` describes `annotation(operation="create_text")` but `floor-plan-draft/SKILL.md` does not enforce it for labels — Claude is free to fall back to drawing.
- No text style is set up at drawing creation. If `STANDARD` text style is broken or text height defaults to a tiny value that disappears at the chosen view, Claude may "see" empty text and switch to line-drawing as a fix.
- No self-check after labeling: Claude never verifies that the entities it created are actually TEXT/MTEXT.

**Fix.** See "Updates to the plugin" §A.

### 2. No blocks — everything built from primitives

**Symptom.** Every fixture, furnishing, window, door leaf, and title-block field is reconstructed line-by-line.

**Why this happens.**
- There is no asset library, so `block(operation="insert", data={"name": "TOILET"})` returns "block not defined" and Claude falls back to drawing.
- `symbol-insert/SKILL.md` explicitly authorizes the primitive fallback. That fallback was meant to be rare — but with zero library it became the default.
- The fallback ad-libs sizes and orientations each time, so the same fixture looks different across rooms.

**Fix.** Build the asset library you and I discussed last turn. Until then, force the symbol-insert skill to *fail loudly* when a block is missing rather than silently drawing primitives.

### 3. Walls are patchwork rectangles

**Symptom.** Exterior wall is a string of small repeated shapes. Interior walls are inconsistent. No double-line cleanup at corners.

**Why this happens.**
- There is no `arch.wall(start, end, thickness)` primitive. The skill instructs Claude to draw two parallel lines and the user has to mentally connect them — but small numerical errors mean the lines drift, and joints overlap.
- The repeated-rectangle pattern is almost certainly Claude trying to represent wall *fill* (poché) by stamping a hatch-like block — which is wrong; wall fill should be a HATCH on `A-WALL-PATT`.

**Fix.** Phase 1.2 in the existing todo list calls for an `arch.wall` tool. Promote this to "do next". Until it's built, rewrite `floor-plan-draft` to *forbid* per-segment wall construction and require a single closed polyline for the exterior with an offset for thickness.

### 4. No dimensions, no callouts, no schedule

**Symptom.** Scale bar only. No dim chain on any wall, no door/window callout bubbles, no schedule.

**Why this happens.**
- The drafting workflow ran `floor-plan-draft` but the user never invoked `dim-annotate` afterward — and the drafting skill doesn't auto-hand-off.
- There is no DIMSTYLE preset, so even if Claude had tried to dimension, the output would have been in raw decimal units with tiny text.

**Fix.** Auto-handoff in `floor-plan-draft` to `dim-annotate` and `drawing-review` as a chained sequence. Add DIMSTYLE setup to the layer/standards bootstrap.

### 5. Crude hatching with no pattern catalog

**Symptom.** Dining rug, kitchen counter, bath floor, beds are hatched with what looks like a custom or default pattern, not a standard architectural one.

**Why this happens.**
- No hatch pattern catalog exposed. Claude doesn't know which pattern to ask for, so it picks `ANSI31` or whatever default and applies it everywhere — or worse, draws the hatch lines manually.

**Fix.** Add a `hatch_patterns` registry to the `arch` tool and a one-liner reference in `floor-plan-draft` mapping material → pattern (carpet → DOLMIT, tile → ANSI37, wood → AR-PARQ1, concrete → AR-CONC).

### 6. Title block is line-geometry text

**Symptom.** "MODERN HOUSE — FLOOR PLAN", "120.4 m²", "2 BED", "1 GUEST", "2 BATH", scale bar — all line-strokes.

**Why this happens.**
- No standard title block was inserted; Claude composed it from scratch.
- `sheet-prep/SKILL.md` mentions a `TBLK_24x36` block but never insists on it. And the floor-plan-draft skill builds a title block inline (which it shouldn't).

**Fix.** Move all title-block construction out of `floor-plan-draft` entirely. Title blocks live in `sheet-prep`. Always insert a real BLOCK with ATTRIB.

### 7. Probable layer drift

**Symptom.** Color discipline suggests Claude tried, but without running `layer-standards-check` we can't confirm NCS compliance.

**Fix.** Add `layer-standards-check` as a *mandatory* final step in `floor-plan-draft` so the audit runs before the human sees the result.

## Root causes, summarized

Three systemic issues produce most of what we see:

1. **The skills permit fallback to primitives without flagging it.** Every "if X isn't available, draw it as lines" instruction in a SKILL is a license for the failure mode in this image. Fallbacks should be loud, opt-in, and reported.
2. **There is no self-verification step.** After Claude drafts, nothing checks that text is TEXT, walls are walls, blocks are blocks. The drawing-review skill exists but isn't auto-chained.
3. **The drawing isn't bootstrapped with standards.** No DIMSTYLE, TEXTSTYLE, MLEADERSTYLE, no standard layer set with the right properties, no title-block template, no annotative scale set. So every Claude turn is invented from zero.

## Recommended fixes — prioritized by impact

These are the changes that fix *this image* most directly. Numbered roughly by ROI.

### P0 — Ship this week

1. **Standards bootstrap on drawing creation.** When a new drawing is created via the plugin, run a setup routine that (a) creates the NCS layer set, (b) defines `Arch-100` and `Arch-50` dimstyles with proper architectural units and text heights, (c) creates a `Standard-Arch` text style with a real arch font (RomanS, City Blueprint, or similar), (d) creates a `Standard-MLeader` multileader style, (e) sets `INSUNITS`, `DIMSCALE`, `CANNOSCALE`. Either via the existing `style` tool or a new `arch.bootstrap_standards()` operation.

2. **Forbid letter-drawing in every skill.** Add this to the top of every drafting SKILL.md: *"You must use `annotation.create_text`, `entity.create_mtext`, or `block.insert_with_attributes` for ALL text. Do not draw letter shapes from lines. If your text doesn't render correctly, fix the text style — do not fall back to line strokes."* Repeat the rule in the subagent prompt.

3. **Loud fallbacks.** Change `symbol-insert/SKILL.md` so that when a block is not found, Claude *stops and reports* "Library asset X not found — install the asset library, or confirm you want a primitive fallback" before drawing. Default to stopping, not drawing.

4. **Auto-handoff chain.** Update `floor-plan-draft` so it ends by automatically invoking `dim-annotate` → `drawing-review`. The user gets the dimensioned, audited drawing in one request instead of three.

5. **Mandatory self-check after each phase.** Add to `floor-plan-draft`: after walls, count entities on `A-WALL-*` layers and confirm they are LWPOLYLINE/LINE (not blocks of strokes). After room tags, confirm each is an MTEXT (not LINE). Surface failures inline, retry once.

### P1 — Ship next week

6. **Asset library** (the one you asked about last turn). Even a minimal seed — 8 doors, 8 windows, 6 plumbing fixtures, 6 furniture pieces, 1 title block, 1 north arrow — kills 90% of the primitive-fallback problem. Schema is already designed.

7. **`arch.wall` primitive.** Single tool call: start, end, thickness, type. Outputs a closed polyline pair plus a centerline on `A-WALL-CNTR`. Cleans intersections via the OVERKILL or FILLET zero-radius trick.

8. **`hatch_patterns` registry + material map.** Material name → pattern name + default scale + default layer. Claude picks `wood-parquet` and gets `AR-PARQ1` on `A-FLOR-PATT` at scale 48 automatically.

9. **`select` operation** (your previous priority list, item 11). Lets the self-check actually find what it's looking for — "select all text on A-ANNO-TEXT", "select all entities on A-WALL-EXTR that aren't LWPOLYLINE".

10. **Layer + dim + text style audit operation** in `drawing-review`. Currently `drawing-review` mostly checks geometry; extend it to check that the standards bootstrap actually took.

### P2 — Within the month

11. **Annotative scaling.** Set `CANNOSCALE` from the requested plot scale on bootstrap. Tag every text/dim entity as annotative so the same drawing plots cleanly at multiple scales.

12. **Title block as a real block** with the `sheet-prep` skill always inserting `TBLK_<size>` and setting attributes — never building it from text or lines.

13. **North arrow, scale bar, graphic scale** as standard blocks in the asset library.

14. **`drawing-review` runs after every Claude turn**, not only when explicitly invoked. Even a quick layer + entity-type spot check is enough to catch text-as-lines and primitives-where-blocks-should-be.

## Updates to the plugin (concrete edits)

### A. `agents/autocad-architect.md` — add to "House rules"

> **Never draw text as line geometry.** All textual content goes through `annotation.create_text`, `entity.create_mtext`, or `block.insert_with_attributes` (for title-block fields). If text does not render, the cause is the text style — fix `TEXTSTYLE`/`DIMTXT`/`MTEXT` height, do not fall back to drawing letters from lines.
>
> **Never reconstruct fixtures, furniture, doors, windows, north arrows, or title blocks from primitives.** Insert from the asset library. If a needed asset is missing, stop and report — do not improvise.
>
> **Never draw walls segment-by-segment.** Use `arch.wall(start, end, thickness, type)` for every wall. If `arch.wall` is unavailable, draw the wall as a single closed polyline per side with `entity.create_polyline` — never as a stack of small rectangles.

### B. `skills/floor-plan-draft/SKILL.md` — chain the workflow

Add at the end of the workflow:

> After step 8 (save), invoke the `dim-annotate` skill, then the `drawing-review` skill, before reporting completion. The deliverable is a *dimensioned, audited* plan — not a bare geometric draft.

Add a Phase 0 standards bootstrap step before laying any geometry:

> **Phase 0 — Bootstrap standards.** Call `arch.bootstrap_standards(units="mm", scale=100)` (or `arch.bootstrap_standards(units="inches", scale=96)`). This creates NCS layers, the `Arch-100` dimstyle, the `Standard-Arch` textstyle, sets `INSUNITS`, `CANNOSCALE`, and inserts the project's default linetypes. Confirm via `layer(operation="list")` and `system(operation="execute_lisp", data={"code": "(getvar \"CANNOSCALE\")"})` before proceeding.

### C. `skills/symbol-insert/SKILL.md` — change the fallback rule

Replace step 5 ("Fall back to primitive geometry if the block is missing") with:

> If the requested block is not in the drawing *and* not in the asset library, **stop**. Report: "Library asset `<name>` not found. Options: (1) install the asset library or extend it, (2) accept a primitive fallback for this drop only — confirm and I'll proceed." Do not silently draw primitives.

### D. `skills/drawing-review/SKILL.md` — add three checks

Add to Category B (Geometry hygiene):

> - **Text-as-lines.** Select all entities on `A-ANNO-*` layers. Anything that should be a label but is LINE/LWPOLYLINE composed of short segments is a critical error — flag it and offer to convert to MTEXT.
> - **Walls-as-rectangles.** Select all entities on `A-WALL-*`. If you find INSERTs of small repeated blocks, or many short LINE segments instead of two LWPOLYLINEs, flag as critical.
> - **Primitives where blocks should be.** For each room, scan for line-built representations of standard fixtures (toilet, sink, bed, table). Flag any that should have been blocks.

### E. New skill `arch-bootstrap` (or operation on `arch` tool)

A single composable bootstrap that any drafting workflow calls first. Includes the NCS layer set, dim/text/mleader styles, a default title-block template insert in paperspace, annotative scale configuration, plot config. Idempotent — running it on an existing drawing should not duplicate layers or styles.

### F. README + decisions doc

Document that:
- DWG is the target file format (per priority); convert via ODA File Converter for headless ops.
- Units default is **millimeters** with scale **1:100** (matches what you drew here) unless overridden.
- The asset library lives at `<workspace>/asset-library/` and is loaded by the `library` MCP tool.

## What this image proves about the system

Three things, stated bluntly:

1. **Tool coverage was not the bottleneck for this drawing — workflow rigor was.** Real AutoCAD was running. The MCP tools exist. Claude had access. The failures are about *which* tools got used and *whether* the result was checked.
2. **The plugin's skills are too permissive.** They describe ideal workflows but list fallbacks that Claude takes whenever the ideal path stutters. Tighten the fallbacks and the output gets dramatically better — even without new code.
3. **Without a self-check loop, Claude can't see what it just drew the way a human reviewer sees it.** A drawing-review pass after every drafting turn is the single largest UX upgrade available, and it costs almost nothing.

## Suggested next move

In priority order:

1. **Edit the four SKILL.md files and the subagent today** to apply the rules above (forbid text-as-lines, forbid primitive fallback in symbol-insert, mandate the standards bootstrap, chain to dim-annotate + drawing-review). This is a 30-minute change with outsized effect — no new code on the MCP server, only better instructions to Claude.
2. **Ship the standards bootstrap.** Either as an `arch.bootstrap_standards` operation on the MCP, or as an `execute_lisp` snippet the skill runs. A day's work.
3. **Seed the asset library.** Even 20 assets total — the bare minimum to cover what's in this plan — will erase most of the primitive-construction problem.
4. **Then everything else from the previous task list** (ODA File Converter, `select`, screenshot cache, COM backend, reactor, project model).

Without the first three, more tools on the MCP won't help — Claude will still take the easy path of drawing letters from lines.
