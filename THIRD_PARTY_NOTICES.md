# Third-party notices

## ZWO ASI SDK

The bundled SDK header, documentation and examples are from ZWO Company and retain their original
[license notice](hsi/lamda_hsi/ASI_linux_mac_SDK_V1.41/license.txt).
Vendor runtime libraries are excluded from version control and must be obtained separately for
physical camera operation. No endorsement by the vendor is implied.

## Other dependencies

Python package dependencies are listed in [requirements.txt](requirements.txt) and retain their own licenses.
The optional legacy ImGui application fetches GLFW, Dear ImGui, ImPlot and stb through its CMake build;
those projects retain their upstream licenses. The normal headless camera target does not fetch that GUI stack.

## Application source

No project-wide license file has been selected for this repository. This notice does not grant a new
license to the application source or replace any bundled third-party notice.
