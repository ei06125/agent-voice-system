# Game Audio and Audio Programming Books: URLs and Companion Repositories

Research date: 2026-08-16

## Copyright and license boundary

Copyright-safe research can include:

- Original summaries of chapters and subjects.
- Topic maps, comparisons, prerequisites, critiques, and reading recommendations.
- Independent explanations of DSP algorithms and programming techniques.
- Newly written pseudocode or implementations based on general concepts.
- Public tables of contents, bibliographic facts, companion links, and repository-license audits.
- Short, necessary, attributed quotations.

It should not reproduce chapters, substantial passages, illustrations, exercises, or enough sequential detail to substitute for a book. Book copyright and companion-code licensing are separate.

URLs are factual information. The list below contains publicly verifiable companion and reference URLs; it is not an exhaustive extraction of every URL printed in every edition.

## Game Audio Programming series

There are currently five volumes:

- [Game Audio Programming: Principles and Practices, Volume 1](https://www.routledge.com/Game-Audio-Programming-Principles-and-Practices/author/p/book/9781498746731)
- [Game Audio Programming 2](https://www.routledge.com/Game-Audio-Programming-2-Principles-and-Practices/Somberg/p/book/9781032401799)
- [Game Audio Programming 3](https://www.routledge.com/Game-Audio-Programming-3-Principles-and-Practices/Somberg/p/book/9780429331251)
- [Game Audio Programming 4](https://www.routledge.com/Game-Audio-Programming-4-Principles-and-Practices/Somberg/p/book/9781032361079)
- [Game Audio Programming 5](https://www.routledge.com/Game-Audio-Programming-5-Principles-and-Practices/Somberg/p/book/9781032852850)

No official unified public GitHub repository for the five-volume series was verified.

Related public material from editor Guy Somberg:

- [How to Build an Audio Engine — GDC slides](https://media.gdcvault.com/gdc2015/presentations/Somberg_Guy_How%20to%20Write.pdf)
- [Fantastic Audio Algorithms and Where to Find Them](https://gdcvault.com/play/1029063/Fantastic-Audio-Algorithms-and-Where)
- [Lessons Learned from a Decade of Audio Programming](https://www.gdcvault.com/play/1020452/Lessons-Learned-from-a-Decade)
- [Lessons Learned from Two Decades of Audio Programming](https://gdcvault.com/play/1028040/Lessons-Learned-From-Two-Decades)
- [Why Game Audio Is Not a Solved Problem](https://www.gdcvault.com/play/1023212/Why-Game-Audio-is-Not)
- [Making Good Tools for Making Great Audio](https://www.gdcvault.com/play/1025185/Making-Good-Tools-for-Making)

These materials discuss audio-engine state machines, threading, listeners, mixing, volumetric sounds, weak pointers, lock-free data transfer, tools, and production constraints.

## Other game-audio books

- [Getting Started with C++ Audio Programming for Game Development](https://www.packtpub.com/en-us/product/getting-started-with-c-audio-programming-for-game-development-9781849699105) — official preview and contents; no official public GitHub repository was verified.
- [Beep to Boom](https://www.routledge.com/Beep-to-Boom-The-Development-of-Advanced-Runtime-Sound-Systems-for-Games-and-Extended-Reality/Goodwin/p/book/9781138543904) — no public code repository was verified.
- [Beep to Boom chapter sampler](https://www.routledge.com/rsc/downloads/GAME_AUDIO_FINAL.pdf)
- [Game Audio Implementation](https://www.gameaudioimplementation.com/) — companion projects and implementation material.
- [Game Audio with FMOD and Unity](https://www.gameaudiobook.com/) — companion site.

## Will Pirkle: effects and synthesizers

### Designing Audio Effect Plugins in C++

- [Official book page](https://www.routledge.com/Designing-Audio-Effect-Plugins-in-C-For-AAX-AU-and-VST3-with-DSP-Theory/Pirkle/p/book/9781138591899)
- [ASPiK repository](https://github.com/willpirkleaudio/ASPiK)
- [ASPiK documentation](https://aspikplugins.com/sdkdocs/html/index.html)
- [ASPiK downloads](https://www.aspikplugins.com/download/)
- [Effects project gallery](https://www.willpirkle.com/fx-book/project-gallery/)

### Designing Software Synthesizer Plugins in C++

- [Official book page](https://www.routledge.com/Designing-Software-Synthesizer-Plugins-in-C-With-Audio-DSP/Pirkle/p/book/9780367510466)
- [SynthLab repository](https://github.com/willpirkleaudio/SynthLab)
- [SynthLab documentation](https://www.willpirkle.com/synthlab/docs/html/index.html)
- [SynthLab downloads](https://www.willpirkle.com/synthlabdm/)

ASPiK and SynthLab describe their licenses as modified BSD but also contain additional SDK restrictions. Do not assume ordinary BSD freedom without reading the complete repository terms. In particular, the repositories distinguish using the SDK to build products from redistributing or repackaging the SDK itself.

## Audio Effects by Reiss and McPherson

- [Audio Effects: Theory, Implementation and Application](https://www.routledge.com/Audio-Effects-Theory-Implementation-and-Application/Reiss-McPherson/p/book/9781032974859)
- [Original SoundSoftware project](https://code.soundsoftware.ac.uk/projects/audio_effects_textbook_code)
- [Updated community implementation by getdunne](https://github.com/getdunne/audio-effects) — GPL-3.0, with documented exceptions for some portions.
- [Community implementation by Juan Gil](https://github.com/juandagilc/Audio-Effects) — GPL-3.0-or-later.

The GitHub projects are community revisions, not unrestricted source for proprietary products. GPL-covered code requires a compatibility and distribution review before integration.

## JUCE tutorial books

- [Creating Synthesizer Plug-Ins with C++ and JUCE repository](https://github.com/TheAudioProgrammer/BuildASynthPluginBook) — MIT source code.
- [Original Hollemans synth repository](https://github.com/hollance/synth-plugin-book) — MIT source code.
- [The Complete Beginner's Guide to Audio Plug-in Development repository](https://github.com/TheAudioProgrammer/BeginnerBookAudioProgramming) — MIT code; fonts and other assets may use separate licenses.
- [The Audio Programmer books and courses](https://www.theaudioprogrammer.com/books-and-courses)
- [JUCE resources](https://juce.com/resources/)

The books remain copyrighted even when their companion source code is MIT-licensed. JUCE itself has separate licensing terms that must be evaluated for the intended product and distribution model.

## DSP and computer-music books

### Think DSP

- [Think DSP repository](https://github.com/AllenDowney/ThinkDSP) — repository code is MIT-licensed.
- [Think DSP second-edition draft](https://github.com/AllenDowney/ThinkDSP2) — MIT repository; work in progress.
- [Think DSP online book](https://greenteapress.com/wp/think-dsp/)

The repository distinguishes the MIT-licensed code from book text distributed under Creative Commons Attribution-NonCommercial-ShareAlike 4.0. Commercial reuse of the prose is therefore different from reuse of the code.

### Digital Audio Theory

- [Companion site](https://digitalaudiotheory.com/)
- [Programming examples on GitLab](https://gitlab.com/dr_bennett/digital-audio-theory)

No clear repository-level license was verified. Treat the examples as copyrighted and unavailable for redistribution or derivative use until the author supplies applicable terms.

### Hack Audio

- [Hack Audio website](https://hackaudio.com/)
- [Hack Audio companion material](https://hackaudio.github.io/)
- [Official book page](https://www.routledge.com/Hack-Audio-An-Introduction-to-Computer-Programming-and-Digital-Signal-Processing/Tarr/p/book/9781138497559)

The companion site states that its material is all rights reserved. Public availability does not imply an open-source license.

### DAFX

- [DAFx book and conference site](https://www.dafx.de/)
- [DAFx open-access paper archive](https://www.dafx.de/paper-archive/)
- [Wiley book page](https://onlinelibrary.wiley.com/doi/book/10.1002/9781119991298)

Conference papers and any accompanying code can have paper-specific or author-specific terms. Verify each artifact independently.

### General audio-programming references

- [The Audio Programming Book](https://mitpress.mit.edu/9780262014465/the-audio-programming-book/)
- [The Computer Music Tutorial, second edition](https://mitpress.mit.edu/9780262044912/the-computer-music-tutorial/)
- [Mathematics of the Discrete Fourier Transform](https://www.dsprelated.com/freebooks/mdft/)
- [Introduction to Digital Filters](https://www.dsprelated.com/freebooks/filters/)
- [Physical Audio Signal Processing](https://www.dsprelated.com/freebooks/pasp/)
- [Spectral Audio Signal Processing](https://www.dsprelated.com/freebooks/sasp/)
- [The Scientist and Engineer's Guide to DSP](https://www.dspguide.com/)

Online reading access is not equivalent to permission to copy the material into documentation, training data, or source repositories. Consult the license or terms on each site.

## AI-enhanced audio plugins

- [Build AI-Enhanced Audio Plugins with C++](https://www.routledge.com/Build-AI-Enhanced-Audio-Plugins-with-C/Yee-King/p/book/9781032430423)
- [Author's support-material entry point](https://www.yeeking.net/book)

The publisher confirms that the book has a freely accessible source repository. The author's support page is the authoritative entry point; an unverified GitHub guess should not be treated as official.

## General game-engine and C++ books

### Game Engine Architecture

- [Official site](https://www.gameenginebook.com/)
- [Official table of contents](https://www.gameenginebook.com/toc.html)

No official companion source repository was verified.

### Game Programming in C++

- [Official Pearson page](https://www.pearson.com/en-us/subject-catalog/p/game-programming-in-c-creating-3d-games/P200000009521/9780134597201)
- [Official source repository](https://github.com/gameprogcpp/code)

The chapter code is BSD-3-Clause. The repository explicitly excludes its `External` directory from that license; every bundled external project must be audited separately.

### Game Programming Patterns

- [Online book](https://gameprogrammingpatterns.com/)
- [Source repository](https://github.com/munificent/game-programming-patterns)

The repository has a mixed license: program source files use MIT, while prose and site files use Creative Commons Attribution-NonCommercial-NoDerivatives 4.0. Do not apply the MIT license to the book text or site design.

### Data-Oriented Design

- [Reduced online edition](https://dataorienteddesign.com/dodbook/)
- [Official source-code repository](https://github.com/raspofabs/dodbooksourcecode/) — MIT.
- [Community-curated Data-Oriented Design resources](https://github.com/dbartolini/data-oriented-design)

The online book and source-code repository are different artifacts. The MIT license identified on the code repository should not automatically be applied to the prose.

### C++ Concurrency in Action

- [Official Manning page](https://www.manning.com/books/c-plus-plus-concurrency-in-action-second-edition)
- [Companion source repository](https://github.com/anthonywilliams/ccia_code_samples) — Boost Software License 1.0.

## Repository licensing checklist

Before copying companion code into a project:

1. Read the root `LICENSE`, `COPYING`, and repository README.
2. Check for per-directory and per-file license notices.
3. Audit submodules, vendored dependencies, SDKs, fonts, samples, presets, images, and impulse responses separately.
4. Distinguish source-code permissions from book-text permissions.
5. Confirm whether attribution or copyright notices must accompany binaries or source distributions.
6. Check whether GPL, LGPL, AGPL, Creative Commons NonCommercial, or NoDerivatives terms conflict with the intended product.
7. Check commercial SDK and plug-in-format terms, including JUCE, AAX, VST3, FMOD, platform SDKs, and third-party model licenses.
8. Record the exact version or commit reviewed; repository licensing can change.
9. Treat a public repository with no license as all rights reserved.
10. Obtain legal review for ambiguous mixed licenses or commercial distribution.

[GitHub's repository-licensing documentation](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository) explains that public visibility alone does not authorize reproduction, distribution, or derivative works. GitHub's platform terms permit viewing and forking, but a separate license is required for broader reuse.
