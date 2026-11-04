# Third-party components

The Windows bundle contains Python, PySide6 / Qt and Shiboken, Pillow,
pillow-heif / libheif, NumPy, OpenCV, ONNX Runtime and their dependencies. Their respective
licenses and package metadata accompany the installer in this directory or in
the bundle. Individual components remain subject to their own license terms.

PySide6 and Qt are available under LGPL-3.0 and other license options; their
libraries are provided separately in the application directory. The LGPL and
GPL texts are included here. Source and build information for the exact PySide6
and Qt releases are available from https://code.qt.io/cgit/pyside/pyside-setup.git/
and https://download.qt.io/official_releases/qt/; package metadata records
the installed versions. Qt third-party acknowledgments are documented at
https://doc.qt.io/qt-6/qt-attribution.html and
https://doc.qt.io/qtforpython-6/licenses.html.

Depth Anything V2 Small INT8 ONNX model by Depth Anything and Open Noodle is
distributed under Apache-2.0. Model: https://huggingface.co/open-noodle/depth-anything-v2-small
revision 7eb4c709bd23da475b4b458582af1686110fd3ca, SHA-256
72e616a3443327db0d9af8b8231deb4f4d14e6fa62b9a4f7dd24d1a7da31df37.
Upstream source: https://github.com/DepthAnything/Depth-Anything-V2. The model
is quantized and packaged as a dependency; photographs are not sent to it over
the network. Apache-2.0 license text is included in this directory.

The application's own source and build instructions are available at
https://github.com/FredMuir/Lumina-Pendant-Prep. No project-wide source license
has yet been selected; this notice does not change third-party license rights.
