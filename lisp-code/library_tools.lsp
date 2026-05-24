;;; library_tools.lsp — Procedural symbol library renderer for AutoCAD LT.
;;;
;;; Parses the compact spec mini-language produced by
;;; `arch/library/apply.py::serialize_spec_for_lisp` and emits the
;;; corresponding LINE / CIRCLE / ARC / PLINE / TEXT primitives. The
;;; mcp_dispatch.lsp `library-define` handler calls these helpers, then
;;; absorbs the entities into a `_.-BLOCK` definition.
;;;
;;; Spec mini-language (entities separated by `;`, fields by `,`):
;;;   L,sx,sy,ex,ey                    LINE
;;;   C,cx,cy,r                         CIRCLE
;;;   A,cx,cy,r,start_deg,end_deg      ARC
;;;   P,closed,n,x1,y1,...,xn,yn       PLINE (closed = 0 | 1)
;;;   T,ix,iy,height,text              TEXT (text is the final field; must
;;;                                          not contain `,` or `;`)
;;;
;;; Loader: mcp_dispatch.lsp auto-loads this file via `findfile`. If the
;;; file isn't on the AutoCAD support path, the dispatcher returns an
;;; actionable error pointing at the `*mcp-library-loaded*` flag.
;;;
;;; Depends on `mcp-split-string` from mcp_dispatch.lsp.
;;;
;;; Compatibility: AutoCAD LT 2024+ for Windows.


(defun mcp-lib-emit-line (parts ss / sx sy ex ey)
  "L spec body: sx, sy, ex, ey  (the type token is consumed by the caller)."
  (setq sx (atof (nth 0 parts))
        sy (atof (nth 1 parts))
        ex (atof (nth 2 parts))
        ey (atof (nth 3 parts)))
  (command "_.LINE" (list sx sy 0.0) (list ex ey 0.0) "")
  (ssadd (entlast) ss))


(defun mcp-lib-emit-circle (parts ss / cx cy r)
  (setq cx (atof (nth 0 parts))
        cy (atof (nth 1 parts))
        r  (atof (nth 2 parts)))
  (command "_.CIRCLE" (list cx cy 0.0) r)
  (ssadd (entlast) ss))


(defun mcp-lib-emit-arc (parts ss / cx cy r start-deg end-deg start-rad)
  ;; AutoCAD ARC `_C` (center) prompt: center, start point, included angle.
  ;; Convert degrees to radians for the start-point trig, then re-use the
  ;; degree-based included angle as the `_A` argument.
  (setq cx (atof (nth 0 parts))
        cy (atof (nth 1 parts))
        r  (atof (nth 2 parts))
        start-deg (atof (nth 3 parts))
        end-deg   (atof (nth 4 parts)))
  (setq start-rad (* start-deg (/ pi 180.0)))
  (command "_.ARC" "_C" (list cx cy 0.0)
           (list (+ cx (* r (cos start-rad)))
                 (+ cy (* r (sin start-rad))) 0.0)
           "_A" (- end-deg start-deg))
  (ssadd (entlast) ss))


(defun mcp-lib-emit-polyline (parts ss / closed-flag n i x y)
  ;; Parts: closed-flag, n, then 2*n coordinates (x1 y1 x2 y2 ...).
  (setq closed-flag (atoi (nth 0 parts))
        n (atoi (nth 1 parts)))
  (command "_.PLINE")
  (setq i 0)
  (while (< i n)
    (setq x (atof (nth (+ 2 (* 2 i)) parts))
          y (atof (nth (+ 3 (* 2 i)) parts)))
    (command (list x y 0.0))
    (setq i (1+ i)))
  ;; Terminate the PLINE: `_C` closes the polyline, empty string ends it open.
  (if (= closed-flag 1)
    (command "_C")
    (command ""))
  (ssadd (entlast) ss))


(defun mcp-lib-emit-text (parts ss / ix iy h text-val)
  ;; Text is the final field. Python validates that the text contains no
  ;; `,` or `;` so the comma-split is safe — index 3 holds the full string.
  (setq ix (atof (nth 0 parts))
        iy (atof (nth 1 parts))
        h  (atof (nth 2 parts))
        text-val (nth 3 parts))
  (command "_.TEXT" (list ix iy 0.0) h 0.0 text-val)
  (ssadd (entlast) ss))


(defun mcp-lib-emit-entities (spec-str / specs ss spec parts kind body)
  "Parse SPEC-STR, draw each entity on the current layer, return a selection set.
   Returns nil when SPEC-STR is empty or no entities were drawn."
  (cond
    ((or (null spec-str) (= spec-str ""))
     nil)
    (t
     (setq specs (mcp-split-string spec-str ";"))
     (setq ss (ssadd))
     (foreach spec specs
       (if (> (strlen spec) 0)
         (progn
           (setq parts (mcp-split-string spec ","))
           (setq kind (car parts))
           (setq body (cdr parts))
           (cond
             ((= kind "L") (mcp-lib-emit-line   body ss))
             ((= kind "C") (mcp-lib-emit-circle body ss))
             ((= kind "A") (mcp-lib-emit-arc    body ss))
             ((= kind "P") (mcp-lib-emit-polyline body ss))
             ((= kind "T") (mcp-lib-emit-text   body ss))
             (t (princ (strcat "\nMCP: unknown library entity type: " kind)))))))
     (if (> (sslength ss) 0) ss nil))))


;; Flag the dispatcher checks before calling `mcp-lib-emit-entities` so the
;; failure mode is an actionable error instead of "no function definition".
(setq *mcp-library-loaded* T)


(princ "\n=== MCP library tools v3.2 loaded ===")
(princ)
