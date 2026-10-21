// Test-only ASI implementation. Never links to the vendor library or accesses USB.
#include "ASICamera2.h"
#include <chrono>
#include <cstring>
#include <cstdlib>
#include <thread>
#include <map>
#include <cstdint>
#include <fstream>
static int width=64, height=32, binning=1, x=0, y=0;
static ASI_IMG_TYPE format=ASI_IMG_RAW16;
static std::map<ASI_CONTROL_TYPE,long> controls{{ASI_EXPOSURE,2000},{ASI_GAIN,100},{ASI_OFFSET,50},{ASI_TEMPERATURE,215}};
static std::chrono::steady_clock::time_point started;
static bool active=false;
static bool removed=false;  // FAKE_ASI_REMOVE_AFTER=<n>: the camera leaves the USB bus at exposure n+1
static int exposures=0;
static bool closed=false;  // FAKE_ASI_FAIL_CLOSE_AFTER=<n>: exposure n+1 fails and the SDK closes the camera
extern "C" {
// FAKE_ASI_NO_CAMERA: enumerate nothing; FAKE_ASI_INIT_ERROR=<code>: ASIInitCamera fails with that
// code; FAKE_ASI_USB2_HOST: report a USB3 camera on a USB2 path; FAKE_ASI_CLOSE_MARKER: file
// written by ASICloseCamera (checks that a failed startup releases the camera).
// FAKE_ASI_EXPOSE_ERROR: exposures fail but the camera stays enumerated (not a USB loss).
int ASIGetNumOfConnectedCameras(){return std::getenv("FAKE_ASI_NO_CAMERA")||removed?0:1;}
ASI_ERROR_CODE ASIGetCameraProperty(ASI_CAMERA_INFO* p,int index){
 if(index!=0)return ASI_ERROR_INVALID_INDEX;
 std::memset(p,0,sizeof(*p));std::strcpy(p->Name,"Test RAW16 camera");
 p->CameraID=0;p->MaxWidth=64;p->MaxHeight=32;p->BitDepth=14;p->PixelSize=3.76;
 p->SupportedBins[0]=1;p->SupportedBins[1]=2;p->SupportedBins[2]=4;
 p->IsUSB3Camera=ASI_TRUE;p->IsUSB3Host=std::getenv("FAKE_ASI_USB2_HOST")?ASI_FALSE:ASI_TRUE;
 return ASI_SUCCESS;
}
ASI_ERROR_CODE ASIOpenCamera(int){return ASI_SUCCESS;}
ASI_ERROR_CODE ASIInitCamera(int){
 if(const char* e=std::getenv("FAKE_ASI_INIT_ERROR"))return static_cast<ASI_ERROR_CODE>(std::atoi(e));
 return ASI_SUCCESS;
}
ASI_ERROR_CODE ASICloseCamera(int){
 if(const char* p=std::getenv("FAKE_ASI_CLOSE_MARKER")){std::ofstream f(p);f<<"closed";}
 return ASI_SUCCESS;
}
ASI_ERROR_CODE ASIGetNumOfControls(int,int* n){*n=0;return ASI_SUCCESS;}
ASI_ERROR_CODE ASIGetControlCaps(int,int,ASI_CONTROL_CAPS*){return ASI_ERROR_INVALID_INDEX;}
ASI_ERROR_CODE ASIGetControlValue(int,ASI_CONTROL_TYPE t,long* v,ASI_BOOL* a){*v=controls[t];*a=ASI_FALSE;return ASI_SUCCESS;}
ASI_ERROR_CODE ASISetControlValue(int,ASI_CONTROL_TYPE t,long v,ASI_BOOL){
 if(const char* e=std::getenv("FAKE_ASI_REJECT_CONTROL"))
  if(t==static_cast<ASI_CONTROL_TYPE>(std::atoi(e)))return ASI_ERROR_GENERAL_ERROR;
 controls[t]=v;return ASI_SUCCESS;
}
ASI_ERROR_CODE ASISetROIFormat(int,int w,int h,int b,ASI_IMG_TYPE t){
 if(closed)return ASI_ERROR_CAMERA_CLOSED;
 if(w<=0||h<=0||w%8||h%2||b<1||w>64/b||h>32/b)return ASI_ERROR_INVALID_SIZE;
 width=w;height=h;binning=b;format=t;return ASI_SUCCESS;
}
ASI_ERROR_CODE ASIGetROIFormat(int,int* w,int* h,int* b,ASI_IMG_TYPE* t){if(closed)return ASI_ERROR_CAMERA_CLOSED;*w=width;*h=height;*b=binning;*t=format;return ASI_SUCCESS;}
ASI_ERROR_CODE ASISetStartPos(int,int a,int b){x=a;y=b;return ASI_SUCCESS;}
ASI_ERROR_CODE ASIGetStartPos(int,int* a,int* b){*a=x;*b=y;return ASI_SUCCESS;}
ASI_ERROR_CODE ASIGetCameraMode(int,ASI_CAMERA_MODE* m){*m=ASI_MODE_NORMAL;return ASI_SUCCESS;}
ASI_ERROR_CODE ASISetCameraMode(int,ASI_CAMERA_MODE){return ASI_SUCCESS;}
ASI_ERROR_CODE ASIStartVideoCapture(int){return ASI_SUCCESS;}
ASI_ERROR_CODE ASIStopVideoCapture(int){return ASI_SUCCESS;}
ASI_ERROR_CODE ASIGetVideoData(int,unsigned char*,long,int){return ASI_ERROR_TIMEOUT;}
ASI_ERROR_CODE ASIStartExposure(int,ASI_BOOL){
 if(std::getenv("FAKE_ASI_EXPOSE_ERROR"))return ASI_ERROR_GENERAL_ERROR;
 if(const char* n=std::getenv("FAKE_ASI_REMOVE_AFTER"))if(exposures>=std::atoi(n))removed=true;
 if(removed)return ASI_ERROR_CAMERA_REMOVED;
 ++exposures;active=true;started=std::chrono::steady_clock::now();return ASI_SUCCESS;
}
ASI_ERROR_CODE ASIStopExposure(int){
 active=false;
 if(const char* p=std::getenv("FAKE_ASI_STOP_MARKER")){std::ofstream f(p);f<<"stopped";}
 return ASI_SUCCESS;
}
ASI_ERROR_CODE ASIGetExpStatus(int,ASI_EXPOSURE_STATUS* s){
 if(std::getenv("FAKE_ASI_HANG"))std::this_thread::sleep_for(std::chrono::seconds(60));
 if(const char* n=std::getenv("FAKE_ASI_FAIL_CLOSE_AFTER"))
  if(active&&exposures>std::atoi(n)){active=false;closed=true;*s=ASI_EXP_FAILED;return ASI_SUCCESS;}
 auto us=std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now()-started).count();
 *s=!active?ASI_EXP_IDLE:us>=controls[ASI_EXPOSURE]?ASI_EXP_SUCCESS:ASI_EXP_WORKING;return ASI_SUCCESS;
}
ASI_ERROR_CODE ASIGetDataAfterExp(int,unsigned char* p,long size){
 if(size<width*height*2)return ASI_ERROR_BUFFER_TOO_SMALL;
 for(int i=0;i<width*height;i++){std::uint16_t v=1000+i;p[2*i]=v&255;p[2*i+1]=v>>8;}
 return ASI_SUCCESS;
}
ASI_ERROR_CODE ASIGetSerialNumber(int,ASI_SN* sn){std::memset(sn,1,sizeof(*sn));return ASI_SUCCESS;}
ASI_ERROR_CODE ASIGetDroppedFrames(int,int* n){*n=0;return ASI_SUCCESS;}
char* ASIGetSDKVersion(){static char version[]="fake-sdk-test-only";return version;}
}
