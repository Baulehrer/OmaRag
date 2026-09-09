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
  s = s.replace(/\b([A-Za-zΑ-Ωα-ω][A-Za-z0-9]{0,2})_([A-Za-z0-9]{1,6}(?:,[A-Za-z0-9]{1,6})?)\b/g,
                "$1<sub>$2</sub>")
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

function toStyled(text) {
  if (!text) return ""
  var s = escapeHtml(text)
  s = greek(s)
  s = scripts(s)
  s = markdown(s)
  return s.replace(/\n/g, "<br>")
}
