# Okular EPUB backend CPU explosion with external stylesheet and many matching elements

## Summary

Okular's EPUB backend exhibits pathological CPU usage when an EPUB contains a sufficiently large number of elements matching a CSS rule in an **external stylesheet**.

I have reduced the problem to a synthetic EPUB containing only:

* one XHTML document
* one external CSS stylesheet
* repeated `<p class="Register">` elements
* the following CSS rule:

```css
p.Register {
  font-size: .85em;
}
```

With this minimal EPUB, Okular behaves normally up to 612 paragraphs, but at 613 paragraphs CPU usage remains around 100% and the document does not finish loading within my timeout.

Changing the CSS to `font-size: 0.85em` avoids the original problem in the real EPUB, although the synthetic test indicates that the pathological behavior depends on the interaction between the external stylesheet, the CSS rule, and the number of matching elements.

## Minimal reproducer

The synthetic EPUB contains this XHTML:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">

<head>
  <title>Synthetic EPUB</title>
  <link rel="stylesheet" type="text/css" href="styles.css"/>
</head>

<body>
  <p class="Register">Paragraph 1</p>
  <p class="Register">Paragraph 2</p>
  <p class="Register">Paragraph 3</p>
  ...
  <p class="Register">Paragraph N</p>
</body>

</html>
```

The external `styles.css` contains only:

```css
p.Register {
  font-size: .85em;
}
```

The EPUB otherwise contains only the normal minimal EPUB structure (`mimetype`, `META-INF/container.xml`, `content.opf`, `content.xhtml`, and `styles.css`).

## Observed threshold

On my system, with a 2-second timeout, I get:

```text
612 paragraphs: GOOD, ~0.70–0.80 s
613 paragraphs: BAD, ~2.01 s and still using ~100% CPU
```

I determined the threshold using exponential search followed by binary search.

Relevant results:

```text
513 paragraphs   GOOD   0.51 s
1025 paragraphs  BAD    2.02 s

769 paragraphs   BAD    2.02 s
641 paragraphs   BAD    2.01 s
577 paragraphs   GOOD   0.81 s
609 paragraphs   GOOD   0.75 s
625 paragraphs   BAD    2.01 s
617 paragraphs   BAD    2.01 s
613 paragraphs   BAD    2.01 s
611 paragraphs   GOOD   0.75 s
612 paragraphs   GOOD   0.70 s
```

Thus the transition is:

**612 → 613 matching paragraphs**

## CPU behaviour

The Okular process remains at approximately one CPU core:

```text
peak CPU ≈ 99–119%
```

The important difference is not the peak CPU value itself, but that the process remains busy instead of becoming idle and finishing normally.

## Additional observations

I originally encountered this with a much larger real EPUB containing approximately 1,000 `<p class="Register">` elements.

The relevant rule in the original EPUB was:

```css
p.Register { 
  text-indent: -1em;
  text-align: left;
  padding-left: 1em;
  font-size: .85em;
}
```

Removing the rule made the EPUB load normally.

I narrowed the problem down to:

```css
font-size: .85em;
```

Changing that to:

```css
font-size: 0.85em;
```

made the original EPUB load normally.

However, a minimal synthetic EPUB using an **inline** stylesheet did not reproduce the pathological behaviour. The critical additional discovery was that moving the rule to an **external stylesheet** reproduces the problem when enough matching elements are present.

For example, with the external stylesheet and `.85em`, load time initially remains roughly constant, but eventually grows rapidly and then hits the pathological behaviour:

```text
257 paragraphs      ~0.50 s
513 paragraphs      ~0.51 s
1025 paragraphs     ~2.02 s / BAD
```

The precise threshold in my minimal reproducer is 613 paragraphs.

## Why I think this is a bug

The EPUB is extremely small and contains no complicated document structure or JavaScript. The only significant operation is applying a simple CSS selector to repeated elements.

The behaviour is also highly non-linear: adding a single matching paragraph changes the document from normal loading to effectively hanging at 100% CPU.

This suggests a possible pathological algorithmic behaviour in CSS parsing, selector matching, style computation, layout, or another part of the EPUB rendering backend.

The fact that `.85em` versus `0.85em` matters in the original EPUB may also indicate a CSS parser/tokenizer issue, although I cannot determine where the problem occurs internally.

## Expected behaviour

The EPUB should load normally regardless of the number of simple paragraphs matching:

```css
p.Register {
  font-size: .85em;
}
```

Increasing the number of paragraphs should result in approximately linear or otherwise reasonable scaling, not a sudden transition into effectively unbounded CPU consumption.

## Actual behaviour

At 613 matching paragraphs in the minimal reproducer, Okular remains at approximately 100% CPU and does not finish within 2 seconds.

With the original EPUB, the same behaviour manifests as Okular consuming a CPU core for a very long time.

## Environment

* OS: NixOS
* Application: Okular
* Format: EPUB
* EPUB backend: EPub

I can provide the minimal synthetic EPUB reproducer and/or the Python script used to generate it if useful.

## Reproduction steps

1. Create an EPUB containing the XHTML and external stylesheet described above.
2. Start with approximately 500 `<p class="Register">` elements.
3. Open the EPUB in Okular.
4. Increase the number of matching paragraphs.
5. Around 613 paragraphs, Okular starts exhibiting pathological CPU usage.
6. At 613 paragraphs, the document no longer finishes loading within my 2-second test timeout.

The same test with an inline `<style>` block does not reproduce the same behaviour.

---

### Minimal test case

The smallest useful reproducer I currently have is:

* `content.xhtml`
* `styles.css`
* minimal `content.opf`
* minimal `container.xml`
* `mimetype`
* 613 `<p class="Register">` elements

I would be happy to attach the generated EPUB and the generator script.
