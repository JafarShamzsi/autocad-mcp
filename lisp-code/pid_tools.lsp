;;; pid_tools.lsp — P&ID helpers for AutoCAD LT via mcp_dispatch.lsp.
;;;
;;; The MCP dispatcher delegates every `pid_*` operation to the `c:` functions
;;; defined here. Without this file the dispatcher returns "pid_tools.lsp
;;; not loaded" — historically the file was referenced but never shipped,
;;; which made the entire `pid` MCP tool silently broken on LT.
;;;
;;; What this file ships:
;;;   - `c:setup-pid-layers` and the eleven `c:` helpers the dispatcher needs.
;;;   - Simple ISA-5.1-flavoured placeholder geometry. Sufficient for an LT
;;;     user without the CTO P&ID Symbol Library installed.
;;;
;;; What this file is NOT:
;;;   - A full CTO replacement. Real ISA-compliant work needs the licensed
;;;     CTO library at C:/PIDv4-CTO/. If that's installed, `c:insert-pid-block`
;;;     uses it; otherwise it draws an outlined-box placeholder.
;;;
;;; Compatibility: AutoCAD LT 2024+ for Windows.

;; Standard PID layer set + suggested colours.
(setq *mcp-pid-layers*
  '(("PID-EQUIPMENT"       6)
    ("PID-PROCESS-PIPING"  4)
    ("PID-UTILITY-PIPING"  3)
    ("PID-INSTRUMENTS"     5)
    ("PID-ELECTRICAL"      1)
    ("PID-ANNOTATION"      7)
    ("PID-VALVES"          2)))


(defun c:setup-pid-layers ( / spec name color)
  "Create the standard P&ID layer set. Idempotent."
  (foreach spec *mcp-pid-layers*
    (setq name (car spec) color (cadr spec))
    (if (not (tblsearch "LAYER" name))
      (command "_.-LAYER" "_M" name "_C" (itoa color) name "")))
  (princ (strcat "\nP&ID layers ready (" (itoa (length *mcp-pid-layers*)) " entries)"))
  (princ))


(defun mcp-pid-set-layer (name)
  "Ensure a layer exists and make it current. Caller restores CLAYER itself."
  (if (not (tblsearch "LAYER" name))
    (command "_.-LAYER" "_M" name ""))
  (setvar "CLAYER" name))


(defun c:draw-process-line (x1 y1 x2 y2 / old)
  "Draw a process line from (x1,y1) to (x2,y2)."
  (setq old (getvar "CLAYER"))
  (mcp-pid-set-layer "PID-PROCESS-PIPING")
  (command "_LINE" (list x1 y1 0.0) (list x2 y2 0.0) "")
  (setvar "CLAYER" old)
  (princ))


(defun c:connect-equipment (x1 y1 x2 y2 / old mid-x)
  "Orthogonal connector between two equipment points (right-angle path)."
  (setq old (getvar "CLAYER"))
  (mcp-pid-set-layer "PID-PROCESS-PIPING")
  (setq mid-x (/ (+ x1 x2) 2.0))
  (command "_PLINE"
           (list x1 y1 0.0)
           (list mid-x y1 0.0)
           (list mid-x y2 0.0)
           (list x2 y2 0.0)
           "")
  (setvar "CLAYER" old)
  (princ))


(defun c:add-flow-arrow (x y rotation / old size rad p-tip p-l p-r)
  "Draw a triangular flow arrow at (x,y), pointing along `rotation` degrees."
  (setq old (getvar "CLAYER"))
  (mcp-pid-set-layer "PID-ANNOTATION")
  (setq size 2.0
        rad (* (/ rotation 180.0) pi))
  ;; Triangle: tip in +rotation direction, base ~120° behind.
  (setq p-tip (list (+ x (* size (cos rad))) (+ y (* size (sin rad))) 0.0))
  (setq p-l (list (+ x (* size 0.5 (cos (+ rad 2.4)))) (+ y (* size 0.5 (sin (+ rad 2.4)))) 0.0))
  (setq p-r (list (+ x (* size 0.5 (cos (- rad 2.4)))) (+ y (* size 0.5 (sin (- rad 2.4)))) 0.0))
  (command "_PLINE" p-tip p-l p-r "_C")
  (setvar "CLAYER" old)
  (princ))


(defun c:add-equipment-tag (x y tag description / old)
  "Place an equipment tag (text) at (x,y); optional description below."
  (setq old (getvar "CLAYER"))
  (mcp-pid-set-layer "PID-ANNOTATION")
  (command "_TEXT" "_J" "_M" (list x y 0.0) 2.5 0.0 tag)
  (if (and description (> (strlen description) 0))
    (command "_TEXT" "_J" "_M" (list x (- y 3.5) 0.0) 1.8 0.0 description))
  (setvar "CLAYER" old)
  (princ))


(defun c:add-line-number (x y line-num spec / old text)
  "Place a piping line-number callout."
  (setq old (getvar "CLAYER"))
  (mcp-pid-set-layer "PID-ANNOTATION")
  (setq text (strcat line-num "-" spec))
  (command "_TEXT" "_J" "_M" (list x y 0.0) 2.0 0.0 text)
  (setvar "CLAYER" old)
  (princ))


(defun c:insert-pid-block (category symbol x y scale rotation / cto-path old size)
  "Insert a P&ID symbol. Prefers the CTO library when available, otherwise
   falls back to a labelled-box placeholder so the operation never silently
   fails."
  (setq cto-path (strcat "C:/PIDv4-CTO/" category "/" symbol ".dwg"))
  (setq old (getvar "CLAYER"))
  (mcp-pid-set-layer "PID-EQUIPMENT")
  (cond
    ((findfile cto-path)
     ;; Real CTO block — load + insert without attribute prompts.
     (setvar "ATTREQ" 0)
     (command "_.-INSERT" cto-path (list x y 0.0) scale scale rotation)
     (setvar "ATTREQ" 1))
    (t
     ;; Placeholder: outlined box + label text. Same shape as the ezdxf
     ;; placeholder so the visual is consistent across backends.
     (setq size (* 5.0 scale))
     (command "_PLINE"
              (list (- x size) (- y size) 0.0)
              (list (+ x size) (- y size) 0.0)
              (list (+ x size) (+ y size) 0.0)
              (list (- x size) (+ y size) 0.0)
              "_C")
     (command "_TEXT" "_J" "_M" (list x y 0.0) (* 1.5 scale) rotation symbol)))
  (setvar "CLAYER" old)
  (princ))


(defun c:insert-valve-on-line (x y valve-type rotation / old size p-l p-r p-t p-b)
  "Diamond-shaped valve symbol centred on (x,y), oriented to `rotation`."
  (setq old (getvar "CLAYER"))
  (mcp-pid-set-layer "PID-VALVES")
  (setq size 3.0)
  ;; Diamond points (left, top, right, bottom). Ignore rotation for the
  ;; diamond itself (rotated diamonds look weird); rotate the label only.
  (setq p-l (list (- x size) y 0.0)
        p-t (list x (+ y size) 0.0)
        p-r (list (+ x size) y 0.0)
        p-b (list x (- y size) 0.0))
  (command "_PLINE" p-l p-t p-r p-b "_C")
  ;; Label below the diamond.
  (command "_TEXT" "_J" "_M" (list x (- y size 2.0) 0.0) 1.5 rotation valve-type)
  (setvar "CLAYER" old)
  (princ))


(defun c:insert-instrument (x y inst-type rotation / old)
  "ISA-5.1 instrument bubble: circle with horizontal divider line."
  (setq old (getvar "CLAYER"))
  (mcp-pid-set-layer "PID-INSTRUMENTS")
  (command "_CIRCLE" (list x y 0.0) 4.0)
  (command "_LINE" (list (- x 4.0) y 0.0) (list (+ x 4.0) y 0.0) "")
  (command "_TEXT" "_J" "_M" (list x (- y 6.0) 0.0) 1.5 rotation inst-type)
  (setvar "CLAYER" old)
  (princ))


(defun c:insert-pump (x y pump-type rotation / old rad tip-x tip-y)
  "Pump: circle with a triangular impeller pointing along `rotation`."
  (setq old (getvar "CLAYER"))
  (mcp-pid-set-layer "PID-EQUIPMENT")
  (command "_CIRCLE" (list x y 0.0) 6.0)
  (setq rad (* (/ rotation 180.0) pi))
  (setq tip-x (+ x (* 8.0 (cos rad)))
        tip-y (+ y (* 8.0 (sin rad))))
  (command "_PLINE"
           (list (+ x (* 6.0 (cos (+ rad 0.5)))) (+ y (* 6.0 (sin (+ rad 0.5)))) 0.0)
           (list tip-x tip-y 0.0)
           (list (+ x (* 6.0 (cos (- rad 0.5)))) (+ y (* 6.0 (sin (- rad 0.5)))) 0.0)
           "_C")
  (command "_TEXT" "_J" "_M" (list x (- y 8.0) 0.0) 1.5 rotation pump-type)
  (setvar "CLAYER" old)
  (princ))


(defun c:insert-tank (x y tank-type scale / old w h)
  "Vertical-tank rectangle scaled around (x,y)."
  (setq old (getvar "CLAYER"))
  (mcp-pid-set-layer "PID-EQUIPMENT")
  (setq w (* 10.0 scale)
        h (* 15.0 scale))
  (command "_PLINE"
           (list (- x w) y 0.0)
           (list (+ x w) y 0.0)
           (list (+ x w) (+ y h) 0.0)
           (list (- x w) (+ y h) 0.0)
           "_C")
  (command "_TEXT" "_J" "_M" (list x (+ y h 2.0) 0.0) (* 2.0 scale) 0.0 tank-type)
  (setvar "CLAYER" old)
  (princ))


(princ "\n=== MCP P&ID tools v3.2 loaded ===")
(princ)
