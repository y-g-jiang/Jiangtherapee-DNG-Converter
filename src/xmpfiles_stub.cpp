

#include <cstdio>
#include <cstdlib>

#define XMPFILES_DEAD(name) \
    extern "C" void name () \
        { \
        fprintf (stderr, "xmpfiles_stub: unreachable %s called\n", #name); \
        abort (); \
        }

XMPFILES_DEAD (WXMPFiles_CanPutXMP_1)
XMPFILES_DEAD (WXMPFiles_CheckFileFormat_1)
XMPFILES_DEAD (WXMPFiles_CheckPackageFormat_1)
XMPFILES_DEAD (WXMPFiles_CloseFile_1)
XMPFILES_DEAD (WXMPFiles_GetAlbumArts_1)
XMPFILES_DEAD (WXMPFiles_GetAssociatedResources_1)
XMPFILES_DEAD (WXMPFiles_GetFileInfo_1)
XMPFILES_DEAD (WXMPFiles_GetFileModDate_1)
XMPFILES_DEAD (WXMPFiles_GetFormatInfo_1)
XMPFILES_DEAD (WXMPFiles_GetXMP_1)
XMPFILES_DEAD (WXMPFiles_IncrementRefCount_1)
XMPFILES_DEAD (WXMPFiles_IsMetadataWritable_1)
XMPFILES_DEAD (WXMPFiles_OpenFile_1)
XMPFILES_DEAD (WXMPFiles_OpenFile_2)
XMPFILES_DEAD (WXMPFiles_PutAlbumArts_1)
XMPFILES_DEAD (WXMPFiles_PutXMP_1)
XMPFILES_DEAD (WXMPFiles_ResetErrorCallbackLimit_1)
XMPFILES_DEAD (WXMPFiles_SetAbortProc_1)
XMPFILES_DEAD (WXMPFiles_SetDefaultErrorCallback_1)
XMPFILES_DEAD (WXMPFiles_SetDefaultProgressCallback_1)
XMPFILES_DEAD (WXMPFiles_SetErrorCallback_1)
XMPFILES_DEAD (WXMPFiles_SetProgressCallback_1)

#include "XMP_Const.h"

extern "C" {

void WXMPFiles_CTor_1 (void *)
{
}

void WXMPFiles_DecrementRefCount_1 (void *)
{
}

void WXMPFiles_GetVersionInfo_1 (XMP_VersionInfo * info)
{
    if (info)
        {
        info->major = 0;
        info->minor = 0;
        info->micro = 0;
        info->isDebug = false;
        info->build = 0;
        info->flags = 0;
        info->message = "XMPFiles stub: not linked";
        }
}

void WXMPFiles_Initialize_1 (void *, XMP_OptionBits)
{
}

void WXMPFiles_Initialize_2 (void *, XMP_OptionBits, XMP_StringPtr, XMP_StringPtr)
{
}

void WXMPFiles_Terminate_1 ()
{
}

}