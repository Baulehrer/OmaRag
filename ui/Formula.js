.pragma library

// Turns what the model writes into something Text.StyledText can draw.
//
// This replaces Text.MarkdownText rather than sitting next to it: the two
// formats are mutually exclusive, and subscripts are worth more here than
// Markdown's own feature set. So the light Markdown the model actually uses —
// bold runs, bullets, headings — is carried over as well.
//
// Order matters. Escaping runs first, so a document full of angle brackets
// cannot smuggle markup through; every tag after that is one we put there.

var GREEK = {
  alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ε", zeta: "ζ",
  eta: "η", theta: "θ", lambda: "λ", mu: "μ", nu: "ν", xi: "ξ", pi: "π",
  rho: "ρ", sigma: "σ", tau: "τ", phi: "φ", chi: "χ", psi: "ψ", omega: "ω",
  Gamma: "Γ", Delta: "Δ", Theta: "Θ", Lambda: "Λ", Xi: "Ξ", Pi: "Π",
  Sigma: "Σ", Phi: "Φ", Psi: "Ψ", Omega: "Ω"
}

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
}

// f_ck · f_{ck,cube} · N/mm^2 · m^3
// Only letters, digits, commas and a comma-separated tail are taken into an
// index — otherwise an underscore in a file name would swallow the rest of the
// sentence.
function scripts(s) {
  s = s.replace(/_\{([^}]{1,20})\}/g, "<sub>$1</sub>")
  s = s.replace(/\^\{([^}]{1,20})\}/g, "<sup>$1</sup>")
  // The base of a formula symbol is short — f, A, σ, Rd. A file name is not.
  // Anchoring on a word boundary and capping the base at three characters
  // keeps `Datei_name.pdf` out of the subscript business.
  //
  // The cap alone was not enough: `lm_studio/ornith-…` came out as `lm` with a
  // subscript `studio`, because the base is two characters and the tail six.
  // What separates a formula from an identifier is its neighbourhood — a slash,
  // a dot, a hyphen or a second underscore next to the match means a path or a
  // model name, never a subscript. Those are excluded on both sides.
  // Two limits, because either alone lets an identifier through. The base is at
  // most two characters and the index at most four — f_ck, c_min, c_nom,
  // Δc_dev, f_yk, E_cm all fit, while max_tokens, chunk_size and top_k do not.
  // And neither side may touch a slash, dot, hyphen or second underscore, which
  // is what turned `lm_studio/ornith-…` into `lm` with a subscript `studio`.
  s = s.replace(/(^|[^\w/.\\-])([A-Za-zΑ-Ωα-ω][A-Za-z0-9]?)_([A-Za-z0-9]{1,4}(?:,[A-Za-z0-9]{1,4})?)(?![\w/.\\-])/g,
                "$1$2<sub>$3</sub>")
  s = s.replace(/([A-Za-zÄÖÜäöüß0-9])\^([A-Za-z0-9]{1,4})\b/g, "$1<sup>$2</sup>")
  return s
}

function greek(s) {
  return s.replace(/\\([A-Za-z]+)/g, function(whole, name) {
    return GREEK[name] !== undefined ? GREEK[name] : whole
  })
}

// The Markdown the model actually produces. Headings become bold lines rather
// than larger text: a heading inside an answer is a label, not a new document.
function markdown(s) {
  s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>")
  s = s.replace(/`([^`\n]+)`/g, "<i>$1</i>")
  s = s.replace(/^#{1,6}\s+(.*)$/gm, "<b>$1</b>")
  s = s.replace(/^\s*[-*]\s+/gm, "• ")
  return s
}

// `lilbee ask` wraps its output to a fixed width whether or not a terminal is
// attached, so the answer arrives pre-broken at some other window's idea of a
// line. Rendering that inside a window with its own width breaks it twice, and
// the result reads like ransom mail.
//
// So the hard wraps come out and the real structure stays: a blank line, a
// heading, a bullet, a numbered item or a table row all begin something and
// keep their break. Anything else is the middle of a sentence and is joined.
function unwrap(text) {
  var lines = String(text).replace(/\r/g, "").split("\n")
  var out = []
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i]
    var starts = /^\s*$/.test(line)
             || /^\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|\||>|```)/.test(line)
    var previous = out.length ? out[out.length - 1] : null
    var joinable = previous !== null && !/^\s*$/.test(previous)
                && !/^\s*(#{1,6}\s|\||```)/.test(previous)
                // A line the wrap broke never ends a paragraph on its own, but
                // one that ends in nothing at all did not come from a wrap.
                && previous.length > 0
    if (!starts && joinable) out[out.length - 1] = previous.replace(/\s+$/, "") + " " + line.replace(/^\s+/, "")
    else out.push(line)
  }
  return out.join("\n")
}

function toStyled(text) {
  if (!text) return ""
  var s = escapeHtml(unwrap(text))
  s = greek(s)
  s = scripts(s)
  s = markdown(s)
  return s.replace(/\n/g, "<br>")
}
