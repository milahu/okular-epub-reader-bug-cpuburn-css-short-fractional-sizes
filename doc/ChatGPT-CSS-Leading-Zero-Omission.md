# CSS Leading Zero Omission

**User:** Anonymous  
**Created:** 2026/9/21 20:49:39  
**Updated:** 2026/9/21 20:50:38  
**Exported:** 2026/9/21 21:05:15  
**Link:** [<https://chatgpt.com/c/6ab17c42-bc24-83ed-9347-5892d1a40856>](https://chatgpt.com/c/6ab17c42-bc24-83ed-9347-5892d1a40856)

## Prompt:

9/21/2026, 8:50:28 PM

in CSS stylesheets, how do we call a short notation like `.5em` instead of the full notation `0.5em`? maybe "short fractional sizes"?

## Response:

9/21/2026, 8:50:28 PM

The usual term is **leading zero omission** (or **omitting the leading zero**).

In CSS, `.5em` is a valid shorthand for `0.5em`. You could describe it as:

- **leading zero omission** — most precise/technical
- **omitting the leading zero** — natural wording
- **leading-zero-less decimal notation** — more formal
- **fractional value notation** — understandable, but less specific

For example:

> “CSS allows **leading zero omission** for decimal values less than 1, so `0.5em` can be written as `.5em`.”

I wouldn't call it “short fractional sizes,” because the technique applies to any decimal number, not specifically sizes: `.75`, `.125em`, `.5s`, etc.

------------------------------------------------------------------------

Powered by [ChatGPT Exporter](https://www.chatgptexporter.com)
