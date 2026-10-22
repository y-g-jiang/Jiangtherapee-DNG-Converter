#include <algorithm>
#include <cstdint>
#include <cstring>
#include <new>
#include <string>

#include "libraw/libraw.h"

#if defined(_WIN32)
#define SRHQ_API extern "C" __declspec(dllexport)
#else
#define SRHQ_API extern "C" __attribute__((visibility("default")))
#endif

namespace {

constexpr uint32_t kAbiVersion = 1;
constexpr const char* kBuildId = "libraw-0.22.2+nikon-he-499bfd4/native-v1";

struct RawHandle {
    LibRaw processor;
    bool opened = false;
    bool unpacked = false;
    std::string error;
};

template <size_t N>
void copy_text(char (&destination)[N], const char* source) {
    std::memset(destination, 0, N);
    if (source) {
        std::strncpy(destination, source, N - 1);
    }
}

void set_error(RawHandle* handle, const char* operation, int code) {
    if (!handle) return;
    handle->error = operation;
    handle->error += " failed: ";
    const char* message = libraw_strerror(code);
    handle->error += message ? message : "unknown LibRaw error";
    handle->error += " (" + std::to_string(code) + ")";
}

bool valid_repeat(const libraw_colordata_t& color, size_t& count) {
    count = static_cast<size_t>(color.cblack[4]) * color.cblack[5];
    return color.cblack[4] > 0 && color.cblack[5] > 0
        && count <= LIBRAW_CBLACK_SIZE - 6;
}

}  // namespace

#pragma pack(push, 8)
struct SrhqRawInfoV1 {
    uint32_t abi_version;
    uint32_t struct_size;
    uint32_t raw_width;
    uint32_t raw_height;
    uint32_t width;
    uint32_t height;
    uint32_t top_margin;
    uint32_t left_margin;
    uint32_t flip;
    uint32_t filters;
    uint32_t colors;
    uint32_t maximum;
    uint32_t data_maximum;
    uint32_t raw_bps;
    uint32_t black_common;
    uint32_t repeat_rows;
    uint32_t repeat_cols;
    uint32_t repeat_count;
    uint32_t repeat_origin_y;
    uint32_t repeat_origin_x;
    int32_t site_color_indices[4];
    float cblack[4];
    float black_site_base[4];
    float black_site[4];
    float cam_mul[4];
    float pre_mul[4];
    float cam_xyz[12];
    float cmatrix[12];
    float rgb_cam[12];
    char cfa_pattern[5];
    char camera_make[64];
    char camera_model[64];
    char unique_camera_model[64];
    char libraw_version[32];
    char build_id[64];
};
#pragma pack(pop)

SRHQ_API uint32_t srhq_joraw2_abi_version() { return kAbiVersion; }
SRHQ_API const char* srhq_joraw2_build_id() { return kBuildId; }
SRHQ_API const char* srhq_joraw2_libraw_version() { return LibRaw::version(); }

SRHQ_API void* srhq_joraw2_create() {
    return new (std::nothrow) RawHandle();
}

SRHQ_API void srhq_joraw2_destroy(void* opaque) {
    delete static_cast<RawHandle*>(opaque);
}

SRHQ_API const char* srhq_joraw2_last_error(void* opaque) {
    auto* handle = static_cast<RawHandle*>(opaque);
    return handle ? handle->error.c_str() : "invalid JoRaw2 handle";
}

SRHQ_API int srhq_joraw2_open_buffer(void* opaque, const void* data, size_t size) {
    auto* handle = static_cast<RawHandle*>(opaque);
    if (!handle || !data || size == 0) return LIBRAW_UNSPECIFIED_ERROR;
    handle->processor.recycle();
    handle->opened = false;
    handle->unpacked = false;
    handle->error.clear();
    const int result = handle->processor.open_buffer(const_cast<void*>(data), size);
    if (result != LIBRAW_SUCCESS) {
        set_error(handle, "open_buffer", result);
        return result;
    }
    handle->opened = true;
    return LIBRAW_SUCCESS;
}

SRHQ_API int srhq_joraw2_unpack(void* opaque) {
    auto* handle = static_cast<RawHandle*>(opaque);
    if (!handle || !handle->opened) return LIBRAW_OUT_OF_ORDER_CALL;
    if (handle->unpacked) return LIBRAW_SUCCESS;
    const int result = handle->processor.unpack();
    if (result != LIBRAW_SUCCESS) {
        set_error(handle, "unpack", result);
        return result;
    }
    handle->unpacked = true;
    return LIBRAW_SUCCESS;
}

SRHQ_API int srhq_joraw2_get_info(void* opaque, SrhqRawInfoV1* output) {
    auto* handle = static_cast<RawHandle*>(opaque);
    if (!handle || !handle->opened || !output) return LIBRAW_OUT_OF_ORDER_CALL;
    if (output->struct_size != sizeof(SrhqRawInfoV1)) return LIBRAW_UNSPECIFIED_ERROR;

    const libraw_data_t& data = handle->processor.imgdata;
    const libraw_image_sizes_t& sizes = data.sizes;
    const libraw_colordata_t& color = data.color;
    const libraw_iparams_t& identity = data.idata;
    std::memset(output, 0, sizeof(*output));
    output->abi_version = kAbiVersion;
    output->struct_size = sizeof(*output);
    output->raw_width = sizes.raw_width;
    output->raw_height = sizes.raw_height;
    output->width = sizes.width;
    output->height = sizes.height;
    output->top_margin = sizes.top_margin;
    output->left_margin = sizes.left_margin;
    output->flip = sizes.flip;
    output->filters = identity.filters;
    output->colors = identity.colors;
    output->maximum = color.maximum;
    output->data_maximum = color.data_maximum;
    output->raw_bps = color.raw_bps;
    output->black_common = color.black;
    output->repeat_origin_y = sizes.top_margin;
    output->repeat_origin_x = sizes.left_margin;

    size_t repeat_count = 0;
    if (valid_repeat(color, repeat_count)) {
        output->repeat_rows = color.cblack[4];
        output->repeat_cols = color.cblack[5];
        output->repeat_count = static_cast<uint32_t>(repeat_count);
    }

    const int row_origin = 8 - (sizes.top_margin & 1);
    const int col_origin = 8 - (sizes.left_margin & 1);
    double repeat_sums[4] = {0.0, 0.0, 0.0, 0.0};
    uint32_t repeat_samples[4] = {0, 0, 0, 0};
    if (repeat_count > 0) {
        const uint32_t period_rows = output->repeat_rows * ((output->repeat_rows & 1) ? 2 : 1);
        const uint32_t period_cols = output->repeat_cols * ((output->repeat_cols & 1) ? 2 : 1);
        for (uint32_t row = 0; row < period_rows; ++row) {
            for (uint32_t col = 0; col < period_cols; ++col) {
                const int site = static_cast<int>(((row + sizes.top_margin) & 1) * 2
                    + ((col + sizes.left_margin) & 1));
                const size_t index = 6 + (row % output->repeat_rows) * output->repeat_cols
                    + (col % output->repeat_cols);
                repeat_sums[site] += color.cblack[index];
                ++repeat_samples[site];
            }
        }
    }

    for (int site = 0; site < 4; ++site) {
        const int row = site / 2;
        const int col = site % 2;
        int channel = handle->processor.COLOR(row_origin + row, col_origin + col);
        if (channel < 0 || channel > 3) channel = 1;
        output->site_color_indices[site] = channel;
        const float base = static_cast<float>(color.black + color.cblack[channel]);
        const float repeat = repeat_samples[site]
            ? static_cast<float>(repeat_sums[site] / repeat_samples[site]) : 0.0f;
        output->black_site_base[site] = base;
        output->black_site[site] = base + repeat;
        const char descriptor = identity.cdesc[channel];
        output->cfa_pattern[site] = descriptor == 'R' || descriptor == 'B' ? descriptor : 'G';
    }
    output->cfa_pattern[4] = '\0';

    for (int channel = 0; channel < 4; ++channel) {
        output->cblack[channel] = static_cast<float>(color.cblack[channel]);
        output->cam_mul[channel] = color.cam_mul[channel];
        output->pre_mul[channel] = color.pre_mul[channel];
    }
    for (int row = 0; row < 4; ++row) {
        for (int col = 0; col < 3; ++col) {
            output->cam_xyz[row * 3 + col] = color.cam_xyz[row][col];
        }
    }
    for (int row = 0; row < 3; ++row) {
        for (int col = 0; col < 4; ++col) {
            output->cmatrix[row * 4 + col] = color.cmatrix[row][col];
            output->rgb_cam[row * 4 + col] = color.rgb_cam[row][col];
        }
    }

    copy_text(output->camera_make, identity.make);
    copy_text(output->camera_model, identity.model);
    copy_text(output->unique_camera_model, color.UniqueCameraModel);
    copy_text(output->libraw_version, LibRaw::version());
    copy_text(output->build_id, kBuildId);
    return LIBRAW_SUCCESS;
}

SRHQ_API size_t srhq_joraw2_black_repeat_count(void* opaque) {
    auto* handle = static_cast<RawHandle*>(opaque);
    if (!handle || !handle->opened) return 0;
    size_t count = 0;
    return valid_repeat(handle->processor.imgdata.color, count) ? count : 0;
}

SRHQ_API int srhq_joraw2_copy_black_repeat(void* opaque, uint16_t* output, size_t capacity) {
    auto* handle = static_cast<RawHandle*>(opaque);
    if (!handle || !handle->opened || !output) return LIBRAW_OUT_OF_ORDER_CALL;
    size_t count = 0;
    const auto& color = handle->processor.imgdata.color;
    if (!valid_repeat(color, count) || capacity < count) return LIBRAW_UNSPECIFIED_ERROR;
    for (size_t index = 0; index < count; ++index) {
        output[index] = static_cast<uint16_t>(color.cblack[6 + index]);
    }
    return LIBRAW_SUCCESS;
}

SRHQ_API const uint16_t* srhq_joraw2_visible_u16_pointer(void* opaque, size_t* pitch_u16) {
    auto* handle = static_cast<RawHandle*>(opaque);
    if (!handle || !handle->unpacked || !pitch_u16) return nullptr;
    const auto& data = handle->processor.imgdata;
    const auto& sizes = data.sizes;
    const uint16_t* raw = data.rawdata.raw_image;
    if (!raw || sizes.width == 0 || sizes.height == 0) return nullptr;
    if (sizes.left_margin + sizes.width > sizes.raw_width
        || sizes.top_margin + sizes.height > sizes.raw_height) {
        return nullptr;
    }
    size_t pitch = sizes.raw_pitch / sizeof(uint16_t);
    if (pitch < sizes.raw_width) pitch = sizes.raw_width;
    *pitch_u16 = pitch;
    return raw + static_cast<size_t>(sizes.top_margin) * pitch + sizes.left_margin;
}

SRHQ_API int srhq_joraw2_copy_visible_u16(void* opaque, uint16_t* output, size_t count) {
    auto* handle = static_cast<RawHandle*>(opaque);
    if (!handle || !handle->unpacked || !output) return LIBRAW_OUT_OF_ORDER_CALL;
    const auto& data = handle->processor.imgdata;
    const auto& sizes = data.sizes;
    const uint16_t* raw = data.rawdata.raw_image;
    if (!raw || sizes.width == 0 || sizes.height == 0) return LIBRAW_FILE_UNSUPPORTED;
    if (count < static_cast<size_t>(sizes.width) * sizes.height) return LIBRAW_UNSPECIFIED_ERROR;
    if (sizes.left_margin + sizes.width > sizes.raw_width
        || sizes.top_margin + sizes.height > sizes.raw_height) {
        return LIBRAW_IO_ERROR;
    }
    size_t pitch = sizes.raw_pitch / sizeof(uint16_t);
    if (pitch < sizes.raw_width) pitch = sizes.raw_width;
    for (uint32_t row = 0; row < sizes.height; ++row) {
        const uint16_t* source = raw + static_cast<size_t>(row + sizes.top_margin) * pitch
            + sizes.left_margin;
        std::memcpy(output + static_cast<size_t>(row) * sizes.width, source,
                    static_cast<size_t>(sizes.width) * sizeof(uint16_t));
    }
    return LIBRAW_SUCCESS;
}
