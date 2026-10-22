

#include "dng_host.h"
#include "dng_image.h"
#include "dng_stream.h"
#include "dng_pixel_buffer.h"
#include "dng_exceptions.h"
#include "dng_jxl.h"

void EncodeJXL_Tile (dng_host &, dng_stream &,
                     const dng_pixel_buffer &,
                     const dng_jxl_color_space_info &,
                     const dng_jxl_encode_settings &)
{
    ThrowNotYetImplemented ();
}

void EncodeJXL_Tile (dng_host &, dng_stream &,
                     const dng_image &,
                     const dng_jxl_color_space_info &,
                     const dng_jxl_encode_settings &)
{
    ThrowNotYetImplemented ();
}

void EncodeJXL_Container (dng_host &, dng_stream &, const dng_image &,
                          const dng_jxl_encode_settings &,
                          const dng_jxl_color_space_info &,
                          const dng_metadata *,
                          const bool, const bool, const bool,
                          const dng_bmff_box_list *)
{
    ThrowNotYetImplemented ();
}

void EncodeJXL_Container (dng_host &, dng_stream &, const dng_pixel_buffer &,
                          const dng_jxl_encode_settings &,
                          const dng_jxl_color_space_info &,
                          const dng_metadata *,
                          const bool, const bool, const bool,
                          const dng_bmff_box_list *)
{
    ThrowNotYetImplemented ();
}

void PreviewColorSpaceToJXLEncoding (const PreviewColorSpaceEnum,
                                     const uint32,
                                     dng_jxl_color_space_info &)
{
    ThrowNotYetImplemented ();
}

bool SupportsJXL (const dng_image &)
{
    return false;
}

bool ParseJXL (dng_host &, dng_stream &, dng_info &, bool, bool)
{
    return false;
}

real32 JXLQualityToDistance (uint32)
{
    ThrowNotYetImplemented ();
    return 0.0f;
}

dng_jxl_encode_settings * JXLQualityToSettings (uint32)
{
    ThrowNotYetImplemented ();
    return NULL;
}

dng_jxl_decoder::~dng_jxl_decoder ()
{
}

void dng_jxl_decoder::Decode (dng_host &, dng_stream &)
{
    ThrowNotYetImplemented ();
}

void dng_jxl_decoder::ProcessExifBox (dng_host &, const std::vector<uint8> &)
{
    ThrowNotYetImplemented ();
}

void dng_jxl_decoder::ProcessXMPBox (dng_host &, const std::vector<uint8> &)
{
    ThrowNotYetImplemented ();
}

void dng_jxl_decoder::ProcessBox (dng_host &, const dng_string &,
                                  const std::vector<uint8> &)
{
    ThrowNotYetImplemented ();
}