# Diagnose EPUB CPU Load

**User:** Anonymous  
**Created:** 2026/9/1 13:41:02  
**Updated:** 2026/9/1 14:12:06  
**Exported:** 2026/9/21 20:41:57  
**Link:** [<https://chatgpt.com/c/6a96b9c7-42f0-83ed-8acc-e52bdd96dfd9>](https://chatgpt.com/c/6a96b9c7-42f0-83ed-8acc-e52bdd96dfd9)

## Prompt:

9/1/2026, 1:40:53 PM

why does the attached EPUB file cause 100% CPU load in all EPUB viewers?

i have tried to remove js/kobo.js but still 100% CPU load... so i suspect there is an HTML fork bomb in that EPUB file...?

\[Attachment: James C. Scott - Die Mühlen der Zivilisation (2019) (9783518761526).epub\]

## Response:

9/1/2026, 1:41:02 PM

> **Inspecting EPUB file metadata and contents**
>
> ------------------------------------------------------------------------
>
> **Analyzing the performance issue**
>
> I’m checking whether the SVG, empty anchors, layout rules, or pagination script could explain sustained CPU use. The most suspicious possibility is an extreme page-count calculation or expensive layout interaction, rather than the small SVG itself.
>
> **Analyzed the performance issue**
>
> **Inspected EPUB structure, assets, styles, scripts, metadata, and link integrity**
>
> **Tested EPUB chapter rendering, simplified XHTML, and font metadata**
>
> **Inspecting EPUB image references**
>
> **Inspected EPUB images, tables, navigation links, and inline styles**
>
> **Testing Chromium with a simple HTML file**
>
> **Tested Chromium with a simple HTML file**
>
> **Searched 1 website**
>
> **Analyzed Unicode combining marks and control characters**
>
> **Searched 10 websites**
>
> Worked for 4m 57s

Yes - I inspected the EPUB itself, and **I don't see an HTML/JavaScript fork bomb**.

What I do see is something more subtle and, in my opinion, a much stronger suspect: **Kobo-specific markup that massively fragments the text into `koboSpan` elements, combined with a Kobo-specific CSS property.**

### What is actually inside this EPUB

There is only one JavaScript file:

`js/kobo.js`

I examined it. It contains pagination/navigation functions, but **no `while(true)`, recursive function, `setInterval`, `setTimeout`, DOM cloning, iframe creation, or other obvious fork-bomb mechanism**.

More importantly, every XHTML document contains things like:

``` html
<span class="koboSpan" id="kobo.1.1">...</span>
<span class="koboSpan" id="kobo.1.2">...</span>
<span class="koboSpan" id="kobo.1.3">...</span>
```

and also:

``` html
<style id="koboSpanStyle">
.koboSpan { -webkit-text-combine: inherit; }
</style>
```

This is very characteristic of Kobo-generated/Kobo-converted EPUB material. There are roughly **1,200 `koboSpan` elements in each of the two appendix files**, and hundreds in most chapters.

I also found that the book is structurally sane in several important respects:

- all XHTML files parse successfully;
- no duplicate IDs within a document;
- no missing internal hyperlink targets;
- no iframes;
- no event-handler JavaScript;
- no SVG apart from the ordinary cover;
- the embedded Linux Libertine OTF fonts are structurally readable;
- images are ordinary JPEGs, with maximum dimensions around 1500 px.

So **this does not look like an intentionally malicious EPUB**.

### The interesting part: `-webkit-text-combine`

The Kobo style is particularly suspicious:

``` css
.koboSpan { -webkit-text-combine: inherit; }
```

`-webkit-text-combine` is an old WebKit implementation of the text-combination feature. WebKit itself has had bugs around this property and its inheritance. ([WebKit Bugzilla](https://bugs.webkit.org/show_bug.cgi?id=234694&utm_source=chatgpt.com))

And the modern equivalent, `text-combine-upright`, is specifically a **layout operation on inline elements**. ([mdn2.netlify.app](https://mdn2.netlify.app/en-us/docs/web/css/text-combine-upright/?utm_source=chatgpt.com))

That means your observation is important:

> removing `kobo.js` doesn't cure the CPU problem.

**That is exactly what I'd expect if the problem is in the HTML/CSS rendering rather than the JavaScript.**

The JS is basically incidental baggage here. In fact, the `kobo.js` code expects elements called `book-columns` and `book-inner`, which **don't exist in these XHTML files**. So in this particular EPUB the script isn't even particularly useful.

### Why could this hit 100% CPU?

The combination is unfortunate:

**normal prose**

→ split into hundreds/thousands of tiny inline boxes

→ every box is a `.koboSpan`

→ every box gets `-webkit-text-combine: inherit`

→ EPUB engine performs pagination/reflow

→ layout engine repeatedly has to resolve/recalculate enormous numbers of inline boundaries.

This isn't an "HTML fork bomb" in the traditional sense. It's closer to a **layout/rendering bomb**: perfectly finite HTML that causes the browser/layout engine to do an absurd amount of work.

And there is some independent evidence that this exact Kobo residue is something people routinely strip when cleaning EPUBs. One recent EPUB-cleaning discussion specifically identifies the combination of `kobo.js`, `koboSpanStyle`, and the `koboSpan` wrappers as Kobo-specific residue. ([天空 Blond](https://skyblond.info/archives/1110.html?utm_source=chatgpt.com))

### The experiment I'd do

Don't just remove `js/kobo.js`.

Remove **all three layers**:

1.  `js/kobo.js`
2.  every `koboSpanStyle` `<style>`
3.  every `<span class="koboSpan" ...>` wrapper, while retaining its text/content.

For example:

``` html
<span class="koboSpan" id="kobo.123.45">some text</span>
```

becomes simply:

``` html
some text
```

The `id="kobo...."` values aren't needed for ordinary EPUB reading.

That is a much more meaningful test than deleting the JS.

**I would expect the resulting EPUB to behave dramatically better if the Kobo markup is the culprit.**

There is another useful clue: the EPUB's metadata explicitly says it is a normal **reflowable EPUB 3**, not some exotic fixed-layout document. So there is no legitimate reason for thousands of Kobo tracking/pagination spans to be necessary for normal reading.

If you want, I can **clean this exact EPUB for you**, preserving the text, footnotes, images, TOC and formatting while removing the Kobo cruft, and give you a repaired `.epub` to test.

------------------------------------------------------------------------

**Sources:**

- [234694 – Make -webkit-text-combine an inherited property](https://bugs.webkit.org/show_bug.cgi?id=234694&utm_source=chatgpt.com)
- [text-combine-upright - CSS: Cascading Style Sheets \| MDN](https://mdn2.netlify.app/en-us/docs/web/css/text-combine-upright/?utm_source=chatgpt.com)
- [〖歪门邪道〗使用LLM翻译超时空辉夜姬轻小说 - 天空 Blond](https://skyblond.info/archives/1110.html?utm_source=chatgpt.com)

## Prompt:

9/1/2026, 2:05:30 PM

> If you want, I can **clean this exact EPUB for you**, preserving the text, footnotes, images, TOC and formatting while removing the Kobo cruft, and give you a repaired `.epub` to test.

no, instead, generate a python script i can run on my machine. it takes the broken epub file path as sys.argv\[1\], and by default, it writes the fixed epub file to the same path as the input file, but the ".epub" extension is replaced with the ".fixed.epub" extension

note: i have tried to remove the CSS style with `sed -i 's/-webkit-text-combine: inherit;//g' OEBPS/*.xhtml` but that has no effect, the EPUB is still causing 100% CPU load in every EPUB viewer

## Response:

9/1/2026, 2:05:31 PM

Yes. And your `sed` experiment is a useful clue: **if removing `-webkit-text-combine` changes nothing, I would not treat that CSS declaration as the primary cause.** The more important thing to remove is the huge number of Kobo `<span class="koboSpan">...</span>` wrappers themselves.

I've generated a standalone Python script that:

- takes the input EPUB as `sys.argv[1]`
- writes `foo.fixed.epub` next to `foo.epub`
- removes `kobo.js`
- removes Kobo's `koboSpanStyle`
- removes `-webkit-text-combine: inherit`
- **unwraps every `koboSpan`**, retaining all text/content
- deliberately avoids XML reserialization, so it doesn't gratuitously rewrite the XHTML
- preserves the EPUB's other files
- keeps `mimetype` uncompressed
- checks that the source ZIP isn't corrupt

[Download `fix_epub.py`](sandbox:/mnt/data/fix_epub.py)

Run it with:

``` bash
python3 fix_epub.py "James C. Scott - Die Mühlen der Zivilisation (2019) (9783518761526).epub"
```

It should produce:

``` text
James C. Scott - Die Mühlen der Zivilisation (2019) (9783518761526).fixed.epub
```

### One important detail

I intentionally **don't use BeautifulSoup/lxml/ElementTree to rewrite the XHTML**. EPUB XHTML can contain things that an XML parser will normalize or rewrite, and there's no reason to introduce that risk just to remove these wrappers.

The key transformation is essentially:

``` html
<span class="koboSpan" id="kobo.1.123">some text</span>
```

→

``` html
some text
```

while leaving the surrounding markup alone.

If this fixed EPUB **still** pins every reader at 100% CPU, that would be a very useful result: at that point I'd look for a different pathological construct in the EPUB—particularly CSS/layout constructs, enormous DOM structures, malformed markup, or a particular XHTML file causing pathological pagination—rather than Kobo JavaScript.

## Prompt:

9/1/2026, 2:08:14 PM

    XHTML files changed       : 28
    koboSpan wrappers removed : 7621
    koboSpanStyle blocks removed: 27
    -webkit-text-combine removed: 0
    kobo.js script references : 28
    kobo.js files removed     : 1

    Done.

but the "fixed" EPUB file is still causing 100% CPU load

## Response:

9/1/2026, 2:08:14 PM

> **Inspecting EPUB archive contents**
>
> ------------------------------------------------------------------------
>
> **Inspected EPUB structure, styles, assets, fonts, XHTML, and MathML anomalies**
>
> **Checking EPUB validation tools**
>
> **Checked EPUB validation tools**
>
> **Diagnosing EPUB performance issues and bisecting content**
>
> **Diagnosed EPUB performance by bisecting content and inspecting font, metadata, and layout settings**
>
> Worked for 1m 15s

That result is actually very useful. **I would now abandon the `koboSpan` hypothesis as the primary cause.** Removing 7,621 wrappers, all Kobo JS, and the Kobo style block without changing the symptom means the pathological behavior is elsewhere.

I re-examined the original EPUB. A few important findings:

- The XHTML documents are **well-formed XML**.
- There is **no `javascript:` URL, iframe, event handler, object/embed, or obvious recursive HTML mechanism**.
- The images are ordinary JPEGs; none are remotely enormous.
- The EPUB is only about **6.2 MB**.
- The CSS is relatively ordinary.
- The embedded LinLibertine fonts are the most interesting remaining non-HTML component.
- There are **28 spine XHTML documents**, with the two appendices being particularly large.
- The OPF contains Kobo metadata, but nothing that resembles an execution loop.

So I think the right next move is **not another blind cleanup**. We should *bisect the EPUB* and find the exact resource that triggers the CPU runaway.

### Most useful test

I recommend making a diagnostic script that generates several EPUB variants:

1.  **No CSS at all**
2.  **No embedded fonts**
3.  **No images**
4.  **No CSS + no fonts + no images**
5.  One EPUB for **each individual XHTML spine document**

That last test is particularly powerful. If, for example, `appendix-001.xhtml` alone produces 100% CPU while every other chapter works, we've immediately narrowed the problem to that file.

If **every individual XHTML file works**, but the complete EPUB doesn't, then the problem is probably in the **OPF/spine/navigation/metadata interaction**, rather than the contents of a particular chapter.

I can generate that diagnostic/bisection script from the attached EPUB, so you can run one command and get a directory of test EPUBs. It will also report exactly which resources it removed from each variant.

## Prompt:

9/1/2026, 2:10:51 PM

> We should *bisect the EPUB* and find the exact resource that triggers the CPU runaway.

yes, i had the same idea

> I recommend making a diagnostic script that generates several EPUB variants

yes, please generate such a script

## Response:

9/1/2026, 2:10:51 PM

It seems like I can’t do more advanced data analysis right now. Please try again later.

------------------------------------------------------------------------

Powered by [ChatGPT Exporter](https://www.chatgptexporter.com)
