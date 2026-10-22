# Third-party components

- The supplied DNG writer uses Adobe DNG SDK 1.7.1, XMP, JPEG and zlib. SDK notices are under licenses. Obtain the SDK source from Adobe for native rebuilding. JPEG XL support is stubbed out by src/jxl_stub.cpp.
- The dynamic LibRaw adapter uses LibRaw 0.22.2, commit b93f6e45c194f5df9b02a43b1af9a54b4f41f33f, with Nikon HE patch head 499bfd4cbf802c2fb5eac4c9a6f53bbeb776cad1. Build identity is in bin/srhq_joraw2-build.json and the ABI bridge source in src/srhq_joraw2_bridge.cpp. Copyright and dual-license texts are included. Upstream: https://github.com/LibRaw/LibRaw . The separately maintained Nikon HE patch and full upstream tree are not bundled.
- NumPy and Pillow are separately installed dependencies under their own licenses.
- Camera records are research data; see README for metadata and lens-matching limitations.
