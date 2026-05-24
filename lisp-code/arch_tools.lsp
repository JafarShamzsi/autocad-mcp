;;; arch_tools.lsp — Generic architectural entity renderer for AutoCAD LT.
;;;
;;; Mirrors the library_tools.lsp pattern, but accepts a per-entity layer
;;; tag so a single spec string can paint a multi-layer composite (stair,
;;; elevation, section, template, etc).
;;;
;;; Spec mini-language (entities separated by `;`, fields by `,`):
;;;   Each entity body is preceded by a layer tag: `@<layer>@<type>,<fields>`
;;;   Types and fields match library_tools.lsp:
;;;     L,sx,sy,ex,ey                    LINE
;;;     C,cx,cy,r                         CIRCLE
;;;     A,cx,cy,r,start_deg,end_deg      ARC
;;;     P,closed,n,x1,y1,...,xn,yn       PLINE (closed = 0 | 1)
;;;     T,ix,iy,height,text              TEXT
;;;
;;; Example (single stair on A-FLOR-STRS):
;;;   "@A-FLOR-STRS@P,1,4,0,0,96,0,96,36,0,36;@A-FLOR-STRS@L,8,0,8,36;..."
;;;
;;; The arch primitives in Python (`autocad_mcp.arch.lisp_render::compose_entities`)
;;; produce this string from the geometry-helper output. The LISP side just
;;; walks it and emits the entities.
;;;
;;; Depends on `mcp-split-string`, `ensure_layer_exists`, `set_current_layer`
;;; from mcp_dispatch.lsp.
;;;
;;; Compatibility: AutoCAD LT 2024+ for Windows.


;; Parse a "@layer@spec" token. Returns (cons layer body) or (cons nil spec)
;; if no layer tag is present.
(defun mcp-arch-extract-layer (token / i layer body)
  (cond
    ((or (null token) (= (strlen token) 0))
     (cons nil token))
    ((/= (substr token 1 1) "@")
     (cons nil token))
    (t
     ;; Walk to the second '@' character.
     (setq i 2)
     (while (and (<= i (strlen token)) (/= (substr token i 1) "@"))
       (setq i (1+ i)))
     (cond
       ((> i (strlen token))
        (cons nil token))
       (t
        (setq layer (substr token 2 (- i 2)))
        (setq body (substr token (+ i 1)))
        (cons layer body))))))


(defun mcp-arch-emit-line (parts / sx sy ex ey)
  (setq sx (atof (nth 0 parts))
        sy (atof (nth 1 parts))
        ex (atof (nth 2 parts))
        ey (atof (nth 3 parts)))
  (command "_.LINE" (list sx sy 0.0) (list ex ey 0.0) "")
  (entlast))


(defun mcp-arch-emit-circle (parts / cx cy r)
  (setq cx (atof (nth 0 parts))
        cy (atof (nth 1 parts))
        r  (atof (nth 2 parts)))
  (command "_.CIRCLE" (list cx cy 0.0) r)
  (entlast))


(defun mcp-arch-emit-arc (parts / cx cy r start-deg end-deg start-rad)
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
  (entlast))


(defun mcp-arch-emit-polyline (parts / closed-flag n i x y)
  (setq closed-flag (atoi (nth 0 parts))
        n (atoi (nth 1 parts)))
  (command "_.PLINE")
  (setq i 0)
  (while (< i n)
    (setq x (atof (nth (+ 2 (* 2 i)) parts))
          y (atof (nth (+ 3 (* 2 i)) parts)))
    (command (list x y 0.0))
    (setq i (1+ i)))
  (if (= closed-flag 1)
    (command "_C")
    (command ""))
  (entlast))


(defun mcp-arch-emit-text (parts / ix iy h text-val)
  (setq ix (atof (nth 0 parts))
        iy (atof (nth 1 parts))
        h  (atof (nth 2 parts))
        text-val (nth 3 parts))
  (command "_.TEXT" (list ix iy 0.0) h 0.0 text-val)
  (entlast))


(defun mcp-arch-emit-entities (spec-str default-layer / specs handles spec layer-pair
                                                  layer body parts kind ent-body i)
  "Walk SPEC-STR, ensure each entity's tagged layer exists, emit it on that
   layer, and return a list of handles. If a spec lacks a layer tag, falls
   back to DEFAULT-LAYER. Layers default to color 7 / CONTINUOUS."
  (setq handles '())
  (cond
    ((or (null spec-str) (= spec-str ""))
     nil)
    (t
     (setq specs (mcp-split-string spec-str ";"))
     (foreach spec specs
       (if (> (strlen spec) 0)
         (progn
           (setq layer-pair (mcp-arch-extract-layer spec))
           (setq layer (car layer-pair) ent-body (cdr layer-pair))
           (if (or (null layer) (= (strlen layer) 0))
             (setq layer default-layer))
           (if (and layer (> (strlen layer) 0))
             (progn
               (if (not (tblsearch "LAYER" layer))
                 (ensure_layer_exists layer 7 "CONTINUOUS"))
               (set_current_layer layer)))
           (setq parts (mcp-split-string ent-body ","))
           (setq kind (car parts))
           (setq body (cdr parts))
           (cond
             ((= kind "L")
              (setq handles (append handles
                              (list (cdr (assoc 5 (entget (mcp-arch-emit-line body))))))))
             ((= kind "C")
              (setq handles (append handles
                              (list (cdr (assoc 5 (entget (mcp-arch-emit-circle body))))))))
             ((= kind "A")
              (setq handles (append handles
                              (list (cdr (assoc 5 (entget (mcp-arch-emit-arc body))))))))
             ((= kind "P")
              (setq handles (append handles
                              (list (cdr (assoc 5 (entget (mcp-arch-emit-polyline body))))))))
             ((= kind "T")
              (setq handles (append handles
                              (list (cdr (assoc 5 (entget (mcp-arch-emit-text body))))))))
             (t (princ (strcat "\nMCP: unknown arch entity type: " kind))))
           )))
     handles)))


;; Dispatcher entry point: parses {"entities": "<spec>", "layer": "<default>"}
;; from a JSON params blob and emits the entities.
(defun mcp-cmd-arch-emit (params / entities default-layer handles json count h i)
  (setq entities (mcp-json-get-string params "entities"))
  (setq default-layer (mcp-json-get-string params "layer"))
  (if (or (null default-layer) (= default-layer "")) (setq default-layer "0"))
  (if (or (null entities) (= entities ""))
    (cons nil "data.entities required")
    (progn
      (setq handles (mcp-arch-emit-entities entities default-layer))
      (setq count (length handles))
      (setq json "[" i 0)
      (foreach h handles
        (if (> i 0) (setq json (strcat json ",")))
        (setq json (strcat json "\"" h "\""))
        (setq i (1+ i)))
      (setq json (strcat json "]"))
      (cons T (strcat "{\"handles\":" json ",\"count\":" (itoa count) "}")))))


;; Flag for the dispatcher.
(setq *mcp-arch-tools-loaded* T)

(princ "\n=== MCP arch tools v3.2 loaded ===")
(princ)
