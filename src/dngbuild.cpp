

#include "dng_assertions.h"
#include "dng_camera_profile.h"
#include "dng_color_space.h"
#include "dng_exceptions.h"
#include "dng_file_stream.h"
#include "dng_host.h"
#include "dng_ifd.h"
#include "dng_image.h"
#include "dng_image_writer.h"
#include "dng_info.h"
#include "dng_linearization_info.h"
#include "dng_matrix.h"
#include "dng_memory.h"
#include "dng_mosaic_info.h"
#include "dng_opcode_list.h"
#include "dng_memory_stream.h"
#include "dng_negative.h"
#include "dng_orientation.h"
#include "dng_simple_image.h"
#include "dng_string.h"
#include "dng_tag_types.h"
#include "dng_tag_values.h"
#include "dng_preview.h"
#include "dng_xmp_sdk.h"
#include "dng_xmp.h"
#include "dng_hue_sat_map.h"
#include <map>
#include <utility>
#include <windows.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <utility>
#include <windows.h>
#include <sstream>
#include <string>
#include <vector>

static std::vector<std::string> split (const std::string &line)
{
    std::vector<std::string> out;
    std::istringstream is (line);
    std::string tok;
    while (is >> tok) out.push_back (tok);
    return out;
}

class PatternNegative : public dng_negative
{
    public:
        dng_point fPatternSize;
        uint8 fPatternData [kMaxCFAPattern * kMaxCFAPattern];
        uint8 fPlaneColor [4];
        bool fUsePattern;

        PatternNegative (dng_host &host)
            : dng_negative (host)
            , fPatternSize (0, 0)
            , fUsePattern (false)
            {
            }

        virtual dng_mosaic_info * MakeMosaicInfo ()
            {
            dng_mosaic_info *info = dng_negative::MakeMosaicInfo ();
            if (info && fUsePattern)
                {
                info->fCFAPatternSize = fPatternSize;
                for (int32 r = 0; r < fPatternSize.v; r++)
                    for (int32 c = 0; c < fPatternSize.h; c++)
                        info->fCFAPattern [r] [c] = fPatternData [r * fPatternSize.h + c];
                for (uint32 j = 0; j < 4; j++)
                    info->fCFAPlaneColor [j] = fPlaneColor [j];
                }
            return info;
            }
};

static bool ReadFileBytes (const char *path, std::vector<uint8_t> &out)
{
    FILE *f = fopen (path, "rb");
    if (!f) return false;
    fseek (f, 0, SEEK_END);
    long n = ftell (f);
    fseek (f, 0, SEEK_SET);
    out.resize ((size_t) n);
    bool ok = fread (out.data (), 1, (size_t) n, f) == (size_t) n;
    fclose (f);
    return ok;
}

class RawJpegPreview : public dng_jpeg_preview
{
    public:
        dng_ifd & IFD () { return fIFD; }
};

static bool LoadHueSatMap (const std::string &path, uint32 h, uint32 s, uint32 v,
                           dng_hue_sat_map &out)
{
    std::vector<uint8_t> bytes;
    if (!ReadFileBytes (path.c_str (), bytes)) return false;
    const size_t count = (size_t) h * s * v;
    if (bytes.size () != count * 3 * sizeof (float)) return false;
    const float *f = (const float *) bytes.data ();
    out.SetDivisions (h, s, v);

    for (uint32 hi = 0; hi < h; hi++)
        for (uint32 si = 0; si < s; si++)
            for (uint32 vi = 0; vi < v; vi++)
                {
                const uint32 tagIndex = ((hi * s + si) * v + vi);
                const uint32 h2 = (tagIndex / s) % h;
                const uint32 s2 = tagIndex % s;
                const uint32 v2 = tagIndex / (s * h);
                dng_hue_sat_map::HSBModify m;
                m.fHueShift = f [(size_t) tagIndex * 3 + 0];
                m.fSatScale = f [(size_t) tagIndex * 3 + 1];
                m.fValScale = f [(size_t) tagIndex * 3 + 2];
                out.SetDelta (h2, s2, v2, m);
                }
    return true;
}

static dng_urational Rat (int32 n, uint32 d)
{
    dng_urational r;
    if (d == 0) { r.Set_real64 (0.0, 1); return r; }
    r.Set_real64 ((real64) n / (real64) d, d);
    return r;
}

int main (int argc, char **argv)
{
    if (argc < 2)
        {
        fprintf (stderr, "usage: dngbuild <spec.txt>\n");
        return 2;
        }

    std::vector<std::vector<std::string> > dirs;
    std::vector<std::string> rawLines;
    {
        FILE *f = fopen (argv [1], "rb");
        if (!f) { fprintf (stderr, "cannot open spec\n"); return 3; }
        char buf [4096];
        while (fgets (buf, sizeof (buf), f))
            {
            std::string line (buf);
            size_t hash = line.find ('#');
            if (hash != std::string::npos) line = line.substr (0, hash);
            dirs.push_back (split (line));
            }
        fclose (f);
    }

    std::string pixelPath, outPath, privPath;
    uint32 pw = 0, ph = 0;
    int phase = -1;
    std::vector<int> pattern6;
    int patternSize = 0;
    bool monochrome = false;
    std::string make, model, local, origname, profileName, calibSignature;
    bool haveActive = false; dng_rect active;
    std::vector<dng_rect> masked;
    bool haveCrop = false; uint32 cropX = 0, cropY = 0, cropW = 0, cropH = 0;
    bool haveScale = false; real64 scaleH = 1.0, scaleV = 1.0;
    bool haveBQ = false; real64 bqScale = 1.0;
    uint32 orientation = 0;
    std::vector<real64> black;
    uint32 blackRepH = 0, blackRepV = 0;
    uint32 whiteLevel = 0;
    bool haveBaselineExposure = false, haveBaselineNoise = false,
         haveBaselineSharpness = false, haveLinLimit = false;
    real64 baselineExposure = 0.0, baselineNoise = 1.0, baselineSharpness = 1.0,
           linLimit = 1.0;
    bool haveShadowScale = false; dng_urational shadowScale = Rat (1, 1);
    bool haveAntiAlias = false; dng_urational antiAlias = Rat (1, 1);
    uint32 bayerGreenSplit = 0;
    std::vector<real64> analogBalance, cameraNeutral;
    bool haveCalib1 = false, haveCalib2 = false;
    dng_matrix calib1 (3, 3), calib2 (3, 3);
    bool haveCM1 = false, haveCM2 = false, haveFM1 = false, haveFM2 = false;
    dng_matrix cm1 (3, 3), cm2 (3, 3), fm1 (3, 3), fm2 (3, 3);
    uint32 illum1 = 0, illum2 = 0;

    bool haveExpTime = false, haveFNumber = false, haveISO = false, haveFocal = false;
    dng_urational expTime = Rat (1, 100), fNumber = Rat (28, 10), focal = Rat (50, 1);
    uint32 isoValue = 0;
    std::string dateTime;
    std::string software, serialNumber, copyright, profileCalibSig, previewDateTime;
    std::string sourceMIME, originalDateTime, originalDocumentID, sourceXMPPath, isoCaptureDate;
    struct XmpProp { std::string ns, prop, value; };
    bool xmpReset = false;
    std::vector<XmpProp> xmpProps;
    std::string lensModel, lensMake, firmware, artist, uniqueModel;
    std::string opcodeList3Path, opcodeList1Path, opcodeList2Path;
    std::string lookTablePath, hueSat1Path, hueSat2Path;
    uint32 lookTableH = 0, lookTableS = 0, lookTableV = 0;
    uint32 hueSat1H = 0, hueSat1S = 0, hueSat1V = 0;
    uint32 hueSat2H = 0, hueSat2S = 0, hueSat2V = 0;
    std::vector<real64> noiseProfile;
    std::vector<dng_urational> lensInfo;
    bool haveImageNumber = false; uint32 imageNumber = 0;
    bool haveDescription = false; std::string imageDescription;
    bool haveDateCreated = false; std::string dateCreated;
    bool haveXmpAlt = false; std::string xmpAltNs, xmpAltProp, xmpAltValue;
    bool haveUserComment = false; std::string userComment;
    bool haveCfaPattern = false; uint32 cfaPatH = 0, cfaPatW = 0;
    uint32 cfaPat [64] = { 0 };

    bool haveOffsetTimeOriginal = false; std::string offsetTimeOriginal;
    bool haveOffsetTimeDigitized = false; std::string offsetTimeDigitized;
    bool haveExposureProgram = false; uint32 exposureProgram = 0;
    bool haveSensitivityType = false; uint32 sensitivityType = 3;
    bool haveSubSec = false; std::string subSec;
    bool haveBrightness = false; dng_srational exifBrightness;

    std::vector<std::pair<uint32, std::pair<uint32, uint32> > > exifU32;
    std::vector<std::pair<uint32, std::pair<uint32, dng_urational> > > exifRat;
    std::vector<std::pair<uint32, dng_srational> > exifSrat;
    bool haveBias=false, haveMaxAp=false, haveMetering=false, haveFlash=false, haveFocalLen=false;
    bool haveColorSpace=false, haveFpX=false, haveFpY=false, haveFpUnit=false;
    bool haveCustomRendered=false, haveExposureMode=false, haveWhiteBalance=false, haveSceneCapture=false;
    dng_srational exifBias; dng_urational exifMaxAp; uint32 exifMetering=0;
    uint32 exifFlash=0, exifFlashMask=0; dng_urational exifFocal; uint32 exifColorSpace=0;
    dng_urational exifFpX, exifFpY; uint32 exifFpUnit=0;
    uint32 exifCustomRendered=0, exifExposureMode=0, exifWhiteBalance=0, exifSceneCapture=0;
    std::string thumbPath, previewPath, appName, appVer, settingsName, settingsDigest;
    uint32 thumbW = 0, thumbH = 0, thumbCh = 3;
    uint32 prevW = 0, prevH = 0, prevSubH = 2, prevSubV = 2;

    for (size_t i = 0; i < dirs.size (); i++)
        {
        const std::vector<std::string> &t = dirs [i];
        if (t.empty ()) continue;
        const std::string &k = t [0];
        auto num = [&] (size_t idx) { return idx < t.size () ? atof (t [idx].c_str ()) : 0.0; };
        auto inum = [&] (size_t idx) { return idx < t.size () ? (uint32) atoi (t [idx].c_str ()) : 0u; };
    auto unum = [&] (size_t idx) { return idx < t.size () ? (uint32) strtoul (t [idx].c_str (), NULL, 10) : 0u; };
        auto text = [&] (size_t from) {
            std::string s;
            for (size_t j = from; j < t.size (); j++) { if (j > from) s += " "; s += t [j]; }
            return s;
        };
        if (k == "pixels") { pixelPath = t [1]; pw = inum (2); ph = inum (3); }
        else if (k == "out") outPath = text (1);
        else if (k == "cfa") phase = (int) inum (1);
        else if (k == "monochrome") monochrome = true;
        else if (k == "artist") artist = text (1);
        else if (k == "uniquemodel") uniqueModel = text (1);
        else if (k == "opcodelist3") opcodeList3Path = t [1];
        else if (k == "opcodelist1") opcodeList1Path = t [1];
        else if (k == "opcodelist2") opcodeList2Path = t [1];
        else if (k == "cfapattern")
            {
            patternSize = (int) inum (1);
            pattern6.clear ();
            for (size_t j = 2; j < t.size (); j++) pattern6.push_back (atoi (t [j].c_str ()));
            }
        else if (k == "make") make = text (1);
        else if (k == "model") model = text (1);
        else if (k == "local") local = text (1);
        else if (k == "origname") origname = text (1);
        else if (k == "profileName") profileName = text (1);
        else if (k == "calibSignature") calibSignature = text (1);
        else if (k == "active") { active = dng_rect ((int32) inum (1), (int32) inum (2), (int32) inum (3), (int32) inum (4)); haveActive = true; }
        else if (k == "masked") masked.push_back (dng_rect ((int32) inum (1), (int32) inum (2), (int32) inum (3), (int32) inum (4)));
        else if (k == "crop") { cropX = inum (1); cropY = inum (2); cropW = inum (3); cropH = inum (4); haveCrop = true; }
        else if (k == "scale") { scaleH = num (1); scaleV = num (2); haveScale = true; }
        else if (k == "bqscale") { bqScale = num (1); haveBQ = true; }
        else if (k == "orientation") orientation = inum (1);
        else if (k == "black")
            {
            uint32 n = inum (1);
            black.clear ();
            for (uint32 j = 0; j < n && 2 + j < t.size (); j++) black.push_back (num (2 + j));
            }
        else if (k == "blackrepeat") { blackRepH = inum (1); blackRepV = inum (2); }
        else if (k == "white") whiteLevel = inum (1);
        else if (k == "baselineExposure") { baselineExposure = num (1); haveBaselineExposure = true; }
        else if (k == "baselineNoise") { baselineNoise = num (1); haveBaselineNoise = true; }
        else if (k == "baselineSharpness") { baselineSharpness = num (1); haveBaselineSharpness = true; }
        else if (k == "linearResponseLimit") { linLimit = num (1); haveLinLimit = true; }
        else if (k == "shadowScale") { shadowScale = Rat ((int32) inum (1), inum (2)); haveShadowScale = true; }
        else if (k == "antiAlias") { antiAlias = Rat ((int32) inum (1), inum (2)); haveAntiAlias = true; }
        else if (k == "bayerGreenSplit") bayerGreenSplit = inum (1);
        else if (k == "analogBalance") { analogBalance.clear (); for (uint32 j = 0; j < inum (1) && 2 + j < t.size (); j++) analogBalance.push_back (num (2 + j)); }
        else if (k == "cameraNeutral") { cameraNeutral.clear (); for (uint32 j = 0; j < inum (1) && 2 + j < t.size (); j++) cameraNeutral.push_back (num (2 + j)); }
        else if (k == "cm1" || k == "cm2" || k == "fm1" || k == "fm2" || k == "calib1" || k == "calib2")
            {
            dng_matrix m (3, 3);
            for (uint32 r = 0; r < 3; r++)
                for (uint32 c = 0; c < 3; c++)
                    {
                    size_t idx = 1 + (r * 3 + c) * 2;
                    m [r] [c] = (idx + 1 < t.size ()) ? ((real64) atoi (t [idx].c_str ()) / (real64) atoi (t [idx + 1].c_str ())) : 0.0;
                    }
            if (k == "cm1") { cm1 = m; haveCM1 = true; }
            else if (k == "cm2") { cm2 = m; haveCM2 = true; }
            else if (k == "fm1") { fm1 = m; haveFM1 = true; }
            else if (k == "fm2") { fm2 = m; haveFM2 = true; }
            else if (k == "calib1") { calib1 = m; haveCalib1 = true; }
            else { calib2 = m; haveCalib2 = true; }
            }
        else if (k == "illum1") illum1 = inum (1);
        else if (k == "illum2") illum2 = inum (1);
        else if (k == "priv") privPath = t [1];
        else if (k == "software") software = text (1);
        else if (k == "lensmodel") lensModel = text (1);
        else if (k == "lensmake") lensMake = text (1);
        else if (k == "firmware") firmware = text (1);
        else if (k == "sourcemime") sourceMIME = text (1);
        else if (k == "originaldate") originalDateTime = text (1);
        else if (k == "originaldocumentid") originalDocumentID = text (1);
        else if (k == "sourcexmp") sourceXMPPath = t [1];
        else if (k == "xmpreset") xmpReset = true;
        else if (k == "xmpnshex")
            {

            XmpProp ph;
            ph.ns = text (1); ph.prop = text (2);
            std::string hx = text (3);
            for (size_t hi = 0; hi + 1 < hx.size (); hi += 2)
                ph.value += (char) (int) strtol (hx.substr (hi, 2).c_str (), NULL, 16);
            xmpProps.push_back (ph);
            }
        else if (k == "xmpns")
            {

            XmpProp p2;
            p2.ns = t [1];
            p2.prop = t [2];
            p2.value = text (3);
            xmpProps.push_back (p2);
            }
        else if (k == "serial") serialNumber = text (1);
        else if (k == "copyright") copyright = text (1);
        else if (k == "copyrighthex")
            {

            copyright.clear ();
            for (size_t j = 1; j < t.size (); j++)
                {
                const std::string &hx = t [j];
                for (size_t b = 0; b + 1 < hx.size (); b += 2)
                    copyright.push_back ((char) strtol (hx.substr (b, 2).c_str (), NULL, 16));
                }
            }
        else if (k == "profilecalibsig") profileCalibSig = text (1);
        else if (k == "imagenumber") { imageNumber = inum (1); haveImageNumber = true; }
        else if (k == "description") { imageDescription = text (1); haveDescription = true; }
        else if (k == "datecreated") { dateCreated = text (1); haveDateCreated = true; }
        else if (k == "usercomment") { userComment = text (1); haveUserComment = true; }
        else if (k == "usercommenthex")
            {
            std::string hx = text (1);
            userComment.clear ();
            for (size_t hi = 0; hi + 1 < hx.size (); hi += 2)
                userComment += (char) (int) strtol (hx.substr (hi, 2).c_str (), NULL, 16);
            haveUserComment = true;
            }
        else if (k == "exifsrat")
            {

            const uint32 code = inum (1);
            dng_srational r; r.n = (int32) unum (2); r.d = (int32) unum (3);
            exifSrat.push_back (std::make_pair (code, r));
            }
        else if (k == "xmpalt") { xmpAltNs = text (1); xmpAltProp = text (2); xmpAltValue = text (3); haveXmpAlt = true; }
        else if (k == "exifcfapattern")
            {
            cfaPatH = inum (1); cfaPatW = inum (2);
            for (uint32 ci = 0; ci < cfaPatH * cfaPatW && 3 + ci < t.size (); ci++)
                cfaPat [ci] = (uint32) inum (3 + ci);
            haveCfaPattern = true;
            }
        else if (k == "offsettimeoriginal") { offsetTimeOriginal = text (1); haveOffsetTimeOriginal = true; }
        else if (k == "offsettimedigitized") { offsetTimeDigitized = text (1); haveOffsetTimeDigitized = true; }
        else if (k == "lens") { lensInfo.clear (); for (size_t j = 1; j + 1 < t.size (); j += 2)
            {
            dng_urational r;
            r.n = (uint32) atoi (t [j].c_str ());
            r.d = (uint32) atoi (t [j + 1].c_str ());
            lensInfo.push_back (r);
            } }
        else if (k == "noise") { noiseProfile.clear (); for (uint32 j = 0; j < inum (1); j++) noiseProfile.push_back (num (2 + j)); }
        else if (k == "looktable") { lookTablePath = t [1]; lookTableH = inum (2); lookTableS = inum (3); lookTableV = inum (4); }
        else if (k == "huesat1") { hueSat1Path = t [1]; hueSat1H = inum (2); hueSat1S = inum (3); hueSat1V = inum (4); }
        else if (k == "huesat2") { hueSat2Path = t [1]; hueSat2H = inum (2); hueSat2S = inum (3); hueSat2V = inum (4); }
        else if (k == "thumb") { thumbPath = t [1]; thumbW = inum (2); thumbH = inum (3); thumbCh = t.size () > 4 ? inum (4) : 3; }
        else if (k == "preview") { previewPath = t [1]; prevW = inum (2); prevH = inum (3); prevSubH = t.size () > 4 ? inum (4) : 2; prevSubV = t.size () > 5 ? inum (5) : 2; }
        else if (k == "appname") appName = text (1);
        else if (k == "appver") appVer = text (1);
        else if (k == "settingsname") settingsName = text (1);
        else if (k == "settingsdigest") settingsDigest = text (1);
        else if (k == "exif")
            {
            const std::string &tag = t [1];
            if (tag == "exposureTime") { expTime = Rat ((int32) inum (2), inum (3)); haveExpTime = true; }
            else if (tag == "fNumber") { fNumber = Rat ((int32) inum (2), inum (3)); haveFNumber = true; }
            else if (tag == "iso") { isoValue = inum (2); haveISO = true; }
            else if (tag == "u32")
                {

                const uint32 code = inum (2);
                const uint32 field = inum (3);
                const uint32 value = inum (4);
                exifU32.push_back (std::make_pair (code, std::make_pair (field, value)));
                }
            else if (tag == "urational")
                {
                const uint32 code = inum (2);
                const uint32 field = inum (3);
                dng_urational r; r.n = inum (4); r.d = inum (5);
                exifRat.push_back (std::make_pair (code, std::make_pair (field, r)));
                }
            else if (tag == "brightness")
                { dng_srational r; r.n = (int32) inum (2); r.d = (int32) inum (3); exifBrightness = r; haveBrightness = true; }
            else if (tag == "subsec") { subSec = text (2); haveSubSec = true; }
            else if (tag == "sensitivityType") { sensitivityType = inum (2); haveSensitivityType = true; }
            else if (tag == "exposureBias")
                { dng_srational r; r.n = (int32) inum (2); r.d = inum (3); exifBias = r; haveBias = true; }
            else if (tag == "maxAperture")
                { dng_urational r; r.n = inum (2); r.d = inum (3); exifMaxAp = r; haveMaxAp = true; }
            else if (tag == "meteringMode") { exifMetering = inum (2); haveMetering = true; }
            else if (tag == "flash") { exifFlash = inum (2); exifFlashMask = inum (3); haveFlash = true; }
            else if (tag == "focalLength")
                { dng_urational r; r.n = inum (2); r.d = inum (3); exifFocal = r; haveFocalLen = true; }
            else if (tag == "colorSpace") { exifColorSpace = inum (2); haveColorSpace = true; }
            else if (tag == "focalPlaneX")
                { dng_urational r; r.n = inum (2); r.d = inum (3); exifFpX = r; haveFpX = true; }
            else if (tag == "focalPlaneY")
                { dng_urational r; r.n = inum (2); r.d = inum (3); exifFpY = r; haveFpY = true; }
            else if (tag == "focalPlaneUnit") { exifFpUnit = inum (2); haveFpUnit = true; }
            else if (tag == "customRendered") { exifCustomRendered = inum (2); haveCustomRendered = true; }
            else if (tag == "exposureMode") { exifExposureMode = inum (2); haveExposureMode = true; }
            else if (tag == "whiteBalance") { exifWhiteBalance = inum (2); haveWhiteBalance = true; }
            else if (tag == "sceneCaptureType") { exifSceneCapture = inum (2); haveSceneCapture = true; }
            else if (tag == "focal") { focal = Rat ((int32) inum (2), inum (3)); haveFocal = true; }
            else if (tag == "exposureProgram") { exposureProgram = inum (2); haveExposureProgram = true; }
            else if (tag == "datetime") { dateTime = text (2); }
            }
        }

    if (pixelPath.empty () || !pw || !ph || outPath.empty ())
        {
        fprintf (stderr, "spec needs pixels and out\n");
        return 4;
        }

    try
        {
        dng_host host;

        dng_xmp_sdk::InitializeSDK (NULL, "Adobe DNG Converter 18.7 (Windows)");

        AutoPtr<PatternNegative> neg (new PatternNegative (host));
        dng_negative *negativeRaw = neg.Get ();
        AutoPtr<dng_negative> negative (neg.Release ());
        PatternNegative *patternNeg = (PatternNegative *) negativeRaw;

        dng_rect frame (0, 0, (int32) ph, (int32) pw);
        AutoPtr<dng_image> image (new dng_simple_image (frame, 1, ttShort,
                                                       host.Allocator ()));
        {
            FILE *f = fopen (pixelPath.c_str (), "rb");
            if (!f) { fprintf (stderr, "cannot open pixels\n"); return 5; }
            std::vector<uint16> row (pw);
            dng_pixel_buffer buf (frame, 0, 1, ttShort, pcPlanar, NULL);
            for (uint32 r = 0; r < ph; r++)
                {
                if (fread (row.data (), 2, pw, f) != pw)
                    {
                    fprintf (stderr, "short read at row %u\n", r);
                    fclose (f);
                    return 6;
                    }
                dng_rect rowRect ((int32) r, 0, (int32) r + 1, (int32) pw);
                dng_pixel_buffer rb (rowRect, 0, 1, ttShort, pcPlanar, NULL);
                rb.fData = row.data ();
                rb.fRowStep = pw;
                rb.fPlaneStep = (int32) (pw * ph);
                image->Put (rb);
                }
            fclose (f);
        }

        if (monochrome)
            {

            negative->SetMonochrome ();
            }
        else if (patternSize > 0 && (int) pattern6.size () == patternSize * patternSize)
            {
            negative->SetRGB ();
            negative->SetColorChannels (3);

            negative->SetBayerMosaic (0);
            dng_mosaic_info *mi = const_cast<dng_mosaic_info *> (negative->GetMosaicInfo ());
            if (!mi) throw dng_exception (dng_error_unknown);
            mi->fCFAPatternSize = dng_point (patternSize, patternSize);
            for (int32 rr = 0; rr < patternSize; rr++)
                for (int32 cc = 0; cc < patternSize; cc++)
                    mi->fCFAPattern [rr] [cc] = (uint8) pattern6 [rr * patternSize + cc];
            mi->fCFAPlaneColor [0] = (uint8) colorKeyRed;
            mi->fCFAPlaneColor [1] = (uint8) colorKeyGreen;
            mi->fCFAPlaneColor [2] = (uint8) colorKeyBlue;
            mi->fCFAPlaneColor [3] = 0xFF;
            }
        else if (phase >= 0)
            {
            negative->SetRGB ();
            negative->SetColorChannels (3);
            negative->SetBayerMosaic ((uint32) phase);
            }

        negative->SetStage1Image (image);

        if (!model.empty ())
            {
            negative->SetModelName (model.c_str ());
            negative->GetExif ()->fModel.Set (model.c_str ());
            }
        if (!local.empty ()) negative->SetLocalName (local.c_str ());
        if (!origname.empty ()) negative->SetOriginalRawFileName (origname.c_str ());
        if (!make.empty ()) negative->GetExif ()->fMake.Set (make.c_str ());
        if (!uniqueModel.empty ()) negative->SetModelName (uniqueModel.c_str ());
        if (haveActive) negative->SetActiveArea (active);
        for (size_t i = 0; i < masked.size (); i++)
            negative->SetMaskedAreas (1, &masked [i]);

        if (!black.empty ())
            {

            for (size_t bi = 0; bi < black.size (); bi++)
                negative->SetBlackLevel (black [bi], (int32) bi);
            if (blackRepV > 1 || blackRepH > 1)
                {
                dng_linearization_info *lin =
                    const_cast<dng_linearization_info *> (negative->GetLinearizationInfo ());
                if (lin)
                    {
                    lin->fBlackLevelRepeatRows = (uint32) blackRepV;
                    lin->fBlackLevelRepeatCols = (uint32) blackRepH;
                    const uint32 planes = negative->ColorChannels ();
                    for (uint32 r = 0; r < (uint32) blackRepV; r++)
                        for (uint32 c = 0; c < (uint32) blackRepH; c++)
                            for (uint32 p = 0; p < planes; p++)
                                {
                                const size_t idx = (size_t) r * blackRepH + c;
                                lin->fBlackLevel [r] [c] [p] = black [idx < black.size () ? idx : 0];
                                }
                    }
                }
            }
        if (whiteLevel) negative->SetWhiteLevel (whiteLevel);

        if (haveCrop)
            {
            negative->SetDefaultCropOrigin (cropX, cropY);
            negative->SetDefaultCropSize (cropW, cropH);
            }
        if (haveScale)
            {
            dng_urational scH; scH.Set_real64 (scaleH, scaleH == (real64) (int32) scaleH ? 1 : 1000);
            dng_urational scV; scV.Set_real64 (scaleV, scaleV == (real64) (int32) scaleV ? 1 : 1000);
            negative->SetDefaultScale (scH, scV);
            }
        if (haveBQ)
            {
            dng_urational bq; bq.Set_real64 (bqScale, bqScale == (real64) (int32) bqScale ? 1 : 1000);
            negative->SetBestQualityScale (bq);
            }
        negative->SetBaseOrientation (dng_orientation::Normal ());
        if (orientation)
            {
            dng_orientation o;
            o = dng_orientation::TIFFtoDNG (orientation);
            negative->SetBaseOrientation (o);
            }

        if (haveBaselineExposure) negative->SetBaselineExposure (baselineExposure);
        if (haveBaselineNoise) negative->SetBaselineNoise (baselineNoise);
        if (haveBaselineSharpness) negative->SetBaselineSharpness (baselineSharpness);
        if (haveLinLimit) negative->SetLinearResponseLimit (linLimit);
        if (haveShadowScale) negative->SetShadowScale (shadowScale);
        if (haveAntiAlias) negative->SetAntiAliasStrength (antiAlias);
        if (bayerGreenSplit) negative->SetGreenSplit (bayerGreenSplit);

        if (!analogBalance.empty ())
            {
            dng_vector v ((uint32) analogBalance.size ());
            for (size_t i = 0; i < analogBalance.size (); i++) v [(uint32) i] = analogBalance [i];
            negative->SetAnalogBalance (v);
            }
        if (!cameraNeutral.empty ())
            {
            dng_vector v ((uint32) cameraNeutral.size ());
            for (size_t i = 0; i < cameraNeutral.size (); i++) v [(uint32) i] = cameraNeutral [i];
            negative->SetCameraNeutral (v);
            }
        if (haveCalib1) negative->SetCameraCalibration1 (calib1);
        if (haveCalib2) negative->SetCameraCalibration2 (calib2);
        if (!calibSignature.empty ()) negative->SetCameraCalibrationSignature (calibSignature.c_str ());

        if (haveCM1)
            {
            AutoPtr<dng_camera_profile> profile (new dng_camera_profile);
            if (!profileName.empty ()) profile->SetName (profileName.c_str ());
            profile->SetEmbedPolicy (pepAllowCopying);
            if (illum1) profile->SetCalibrationIlluminant1 (illum1);
            if (illum2) profile->SetCalibrationIlluminant2 (illum2);
            profile->SetColorMatrix1 (cm1);
            if (haveCM2) profile->SetColorMatrix2 (cm2);
            if (haveFM1) profile->SetForwardMatrix1 (fm1);
            if (haveFM2) profile->SetForwardMatrix2 (fm2);
            if (!copyright.empty ()) profile->SetCopyright (copyright.c_str ());
            if (!profileCalibSig.empty ()) profile->SetProfileCalibrationSignature (profileCalibSig.c_str ());
            {
            dng_hue_sat_map m;
            if (!hueSat1Path.empty () && LoadHueSatMap (hueSat1Path, hueSat1H, hueSat1S, hueSat1V, m))
                profile->SetHueSatDeltas1 (m);
            if (!hueSat2Path.empty () && LoadHueSatMap (hueSat2Path, hueSat2H, hueSat2S, hueSat2V, m))
                profile->SetHueSatDeltas2 (m);
            if (!lookTablePath.empty () && LoadHueSatMap (lookTablePath, lookTableH, lookTableS, lookTableV, m))
                {
                profile->SetLookTable (m);
                }
            }
            negative->AddProfile (profile);
            }

        if (!privPath.empty ())
            {
            FILE *f = fopen (privPath.c_str (), "rb");
            if (f)
                {
                fseek (f, 0, SEEK_END);
                long n = ftell (f);
                fseek (f, 0, SEEK_SET);
                AutoPtr<dng_memory_block> block (host.Allocate ((uint32) n));
                if (fread (block->Buffer (), 1, (size_t) n, f) == (size_t) n)
                    {
                    AutoPtr<dng_memory_block> copy (block.Release ());
                    negative->SetPrivateData (copy);
                    }
                fclose (f);
                }
            }

        if (!noiseProfile.empty ())
            {
            dng_std_vector<dng_noise_function> funcs;
            for (size_t i = 0; i + 1 < noiseProfile.size (); i += 2)
                funcs.push_back (dng_noise_function (noiseProfile [i], noiseProfile [i + 1]));
            dng_noise_profile np (funcs);
            if (np.IsValid ()) negative->SetNoiseProfile (np);
            }

        dng_exif *exif = negative->GetExif ();
        if (!originalDateTime.empty ())
            {

            isoCaptureDate = originalDateTime;
            for (size_t j = 0; j < isoCaptureDate.size (); j++)
                {
                if (j == 4 || j == 7) isoCaptureDate [j] = '-';
                else if (j == 10) isoCaptureDate [j] = 'T';
                }
            dng_date_time odt;
            if (odt.Parse (originalDateTime.c_str ()))
                {
                exif->fDateTimeOriginal.SetDateTime (odt);
                exif->fDateTimeDigitized.SetDateTime (odt);
                }
            }
        if (!dateTime.empty ())
            {

            char iso [64];
            int y = 0, mo = 0, d = 0, hh = 0, mi = 0, ss = 0;
            if (sscanf (dateTime.c_str (), "%d:%d:%d %d:%d:%d", &y, &mo, &d, &hh, &mi, &ss) == 6)
                {
                TIME_ZONE_INFORMATION tz;
                DWORD tzi = GetTimeZoneInformation (&tz);
                long bias = tz.Bias + (tzi == TIME_ZONE_ID_DAYLIGHT ? tz.DaylightBias : 0);
                long off = -bias;
                snprintf (iso, sizeof (iso), "%04d-%02d-%02dT%02d:%02d:%02d%+03ld:%02ld",
                          y, mo, d, hh, mi, ss, off / 60, labs (off % 60));
                previewDateTime = iso;
                }
            }
        if (!software.empty ()) exif->fSoftware.Set (software.c_str ());
        if (!serialNumber.empty ())
            {
            exif->fCameraSerialNumber.Set (serialNumber.c_str ());
            }
        if (!lensInfo.empty ())
            {
            for (size_t i = 0; i < lensInfo.size () && i < 4; i++) exif->fLensInfo [i] = lensInfo [i];
            }
        if (haveImageNumber) exif->fImageNumber = imageNumber;

        if (haveDescription) exif->fImageDescription.Set (imageDescription.c_str ());
        if (haveUserComment) exif->fUserComment.Set (userComment.c_str ());
        if (!lensModel.empty ())
            {
            exif->fLensName.Set (lensModel.c_str ());
            exif->fLensNameWasReadFromExif = true;
            }
        if (!lensMake.empty ()) exif->fLensMake.Set (lensMake.c_str ());
        if (!firmware.empty ()) exif->fFirmware.Set (firmware.c_str ());
        if (!artist.empty ()) exif->fArtist.Set (artist.c_str ());
        if (haveExpTime) exif->fExposureTime = expTime;
        if (haveFNumber) exif->fFNumber = fNumber;
        if (haveExposureProgram) exif->fExposureProgram = exposureProgram;
        if (haveBias) exif->fExposureBiasValue = exifBias;
        if (haveMaxAp) exif->fMaxApertureValue = exifMaxAp;
        if (haveMetering) exif->fMeteringMode = exifMetering;
        if (haveFlash) { exif->fFlash = exifFlash; exif->fFlashMask = exifFlashMask; }
        if (haveFocalLen) exif->fFocalLength = exifFocal;
        if (haveColorSpace) exif->fColorSpace = exifColorSpace;
        if (haveFpX) exif->fFocalPlaneXResolution = exifFpX;
        if (haveFpY) exif->fFocalPlaneYResolution = exifFpY;
        if (haveFpUnit) exif->fFocalPlaneResolutionUnit = exifFpUnit;
        if (haveCustomRendered) exif->fCustomRendered = exifCustomRendered;
        if (haveExposureMode) exif->fExposureMode = exifExposureMode;
        if (haveWhiteBalance) exif->fWhiteBalance = exifWhiteBalance;
        if (haveSceneCapture) exif->fSceneCaptureType = exifSceneCapture;

        for (size_t j = 0; j < exifU32.size (); j++)
            {
            const uint32 code = exifU32 [j].first;
            const uint32 value = exifU32 [j].second.second;
            switch (code)
                {
                case 0xA405: exif->fFocalLengthIn35mmFilm = value; break;
                case 0xA408: exif->fContrast = value; break;
                case 0xA409: exif->fSaturation = value; break;
                case 0xA40A: exif->fSharpness = value; break;
                case 0x9208: exif->fLightSource = value; break;
                case 0xA300: exif->fFileSource = value; break;
                case 0xA301: exif->fSceneType = value; break;
                case 0x8832: exif->fRecommendedExposureIndex = value; break;
                case 0xA40C: exif->fSubjectDistanceRange = value; break;
                case 0xA217: exif->fSensingMethod = value; break;
                case 0x8831: exif->fStandardOutputSensitivity = value; break;
                case 0xA407: exif->fGainControl = value; break;
                default: break;
                }
            }
        for (size_t j = 0; j < exifSrat.size (); j++)
            switch (exifSrat [j].first)
                {
                case 0x9400: exif->fTemperature = exifSrat [j].second; break;
                case 0x9402: exif->fPressure.n = exifSrat [j].second.n;
                             exif->fPressure.d = (uint32) exifSrat [j].second.d; break;
                case 0x9403: exif->fWaterDepth = exifSrat [j].second; break;
                case 0x9404: exif->fAcceleration.n = exifSrat [j].second.n; exif->fAcceleration.d = exifSrat [j].second.d; break;
                case 0x9405: exif->fCameraElevationAngle = exifSrat [j].second; break;
                default: break;
                }
        for (size_t j = 0; j < exifRat.size (); j++)
            {
            if (exifRat [j].first == 0xA404) exif->fDigitalZoomRatio = exifRat [j].second.second;
            if (exifRat [j].first == 0x9206) exif->fSubjectDistance = exifRat [j].second.second;
            }
        if (haveBrightness) exif->fBrightnessValue = exifBrightness;
        if (haveSubSec) exif->fDateTimeOriginal.SetSubseconds (dng_string (subSec.c_str ()));
        if (haveISO)
            {
            exif->fISOSpeedRatings [0] = (uint16) isoValue;
            exif->fSensitivityType = haveSensitivityType ? sensitivityType : 0;
            }
        if (haveFocal) exif->fFocalLength = focal;
        if (!dateTime.empty ())
            {
            dng_date_time parsed;
            if (parsed.Parse (dateTime.c_str ()))
                {
                exif->fDateTime.SetDateTime (parsed);
                }
            }

        if (!sourceMIME.empty ()) negative->SetSourceMIME (sourceMIME.c_str ());

        if (!sourceXMPPath.empty ())
            {
            std::vector<uint8_t> bytes;
            if (ReadFileBytes (sourceXMPPath.c_str (), bytes) && !bytes.empty ())
                {
                negative->Metadata ().SetEmbeddedXMP (host, bytes.data (), (uint32) bytes.size ());
                if (dng_xmp *xr = negative->GetXMP ())
                    xr->Remove ("http:
                }
            }

        for (int oi = 0; oi < 2; oi++)
            {
            const std::string &p = oi == 0 ? opcodeList1Path : opcodeList2Path;
            if (p.empty ()) continue;
            std::vector<uint8_t> bytes;
            if (ReadFileBytes (p.c_str (), bytes) && bytes.size () > 4)
                {
                dng_memory_stream stream (host.Allocator ());
                stream.Put (bytes.data (), (uint32) bytes.size ());
                stream.SetReadPosition (0);
                (oi == 0 ? negative->OpcodeList1 () : negative->OpcodeList2 ())
                    . Parse (host, stream, (uint32) bytes.size (), 0);
                }
            }

        if (!opcodeList3Path.empty ())
            {
            std::vector<uint8_t> bytes;
            if (ReadFileBytes (opcodeList3Path.c_str (), bytes) && bytes.size () > 4)
                {
                dng_memory_stream stream (host.Allocator ());
                stream.Put (bytes.data (), (uint32) bytes.size ());
                stream.SetReadPosition (0);
                negative->OpcodeList3 (). Parse (host, stream, (uint32) bytes.size (), 0);
                fprintf (stderr, "opcode list 3: %u opcodes, %u bytes\n",
                         (unsigned) negative->OpcodeList3 (). Count (), (unsigned) bytes.size ());
                }
            }

        if (dng_xmp *xp = negative->GetXMP ())
            {
            if (!isoCaptureDate.empty ())
                {
                xp->Set ("http:
                xp->Set ("http:
                }
            if (!originalDocumentID.empty ())
                {
                xp->Set ("http:
                         originalDocumentID.c_str ());
                xp->Set ("http:
                         originalDocumentID.c_str ());
                }
            for (size_t j = 0; j < xmpProps.size (); j++)
                xp->Set (xmpProps [j].ns.c_str (), xmpProps [j].prop.c_str (),
                         xmpProps [j].value.c_str ());
            }

        negative->SynchronizeMetadata ();

        if (!previewDateTime.empty ())
            if (dng_xmp *xm = negative->GetXMP ())
                xm->Set ("http:
        if (negative->GetExif ())
            {
            if (haveSubSec)
                negative->GetExif ()->fDateTimeOriginal.SetSubseconds (dng_string (subSec.c_str ()));
            if (haveCfaPattern)
                {

                negative->GetExif ()->fCFARepeatPatternRows = cfaPatH;
                negative->GetExif ()->fCFARepeatPatternCols = cfaPatW;
                for (uint32 cy = 0; cy < cfaPatH && cy < 8; cy++)
                    for (uint32 cx = 0; cx < cfaPatW && cx < 8; cx++)
                        negative->GetExif ()->fCFAPattern [cy][cx] = (uint8) cfaPat [cy * cfaPatW + cx];
                }
            if (haveXmpAlt)
                if (dng_xmp *xa = negative->GetXMP ())
                    xa->SetAltLangDefault (xmpAltNs.c_str (), xmpAltProp.c_str (),
                                           xmpAltValue.c_str ());
            if (haveDateCreated)
                if (dng_xmp *xd = negative->GetXMP ())
                    xd->Set ("http:
                             dateCreated.c_str ());
            if (!software.empty ())
                {
                negative->GetExif ()->fSoftware.Set (software.c_str ());
                if (dng_xmp *xs = negative->GetXMP ())
                    xs->Set ("http:
                fprintf (stderr, "software set to '%s'; metadata exif is %s; md software '%s'\n",
                         software.c_str (),
                         (negative->GetExif () == negative->Metadata ().GetExif ()) ? "same" : "DIFFERENT",
                         negative->Metadata ().GetExif ()->fSoftware.Get ());
                }
            if (haveDescription)
                {
                negative->GetExif ()->fImageDescription.Set (imageDescription.c_str ());
                fprintf (stderr, "description set to '%s' (%d chars)\n",
                         negative->GetExif ()->fImageDescription.Get (),
                         (int) negative->GetExif ()->fImageDescription.Length ());
                }
            if (haveOffsetTimeOriginal)
                negative->GetExif ()->fDateTimeOriginal.SetOffsetTime (
                    dng_string (offsetTimeOriginal.c_str ()));
            if (haveOffsetTimeDigitized)
                negative->GetExif ()->fDateTimeDigitized.SetOffsetTime (
                    dng_string (offsetTimeDigitized.c_str ()));
            }

        {
        const dng_point dfs (negative->DefaultFinalHeight (), negative->DefaultFinalWidth ());
        negative->SetOriginalDefaultFinalSize (dfs);
        negative->SetOriginalBestQualityFinalSize (dfs);
        dng_urational cH; cH.Set_real64 (dfs.h, 1);
        dng_urational cV; cV.Set_real64 (dfs.v, 1);
        negative->SetOriginalDefaultCropSize (cH, cV);
        }

        AutoPtr<dng_preview_list> previews (new dng_preview_list);

        auto fillInfo = [] (dng_preview_info &info, const std::string &appName,
                            const std::string &appVer, const std::string &setName,
                            const std::string &digest, const std::string &dtStr)
            {
            info.fIsPrimary = true;
            if (!appName.empty ()) info.fApplicationName.Set (appName.c_str ());
            if (!appVer.empty ()) info.fApplicationVersion.Set (appVer.c_str ());
            if (!setName.empty ()) info.fSettingsName.Set (setName.c_str ());
            info.fColorSpace = previewColorSpace_sRGB;
            if (!dtStr.empty ()) info.fDateTime.Set (dtStr.c_str ());
            if (digest.size () == 32) info.fSettingsDigest = dng_fingerprint (digest.c_str ());
            };

        if (!thumbPath.empty ())
            {
            std::vector<uint8_t> bytes;
            if (!ReadFileBytes (thumbPath.c_str (), bytes))
                { fprintf (stderr, "cannot read thumbnail\n"); return 9; }
            if (bytes.size () != (size_t) thumbW * thumbH * thumbCh)
                { fprintf (stderr, "thumb size mismatch\n"); return 9; }
            AutoPtr<dng_image> image (new dng_simple_image (dng_rect (0, 0, (int32) thumbH, (int32) thumbW),
                                                           thumbCh, ttByte, host.Allocator ()));
            dng_pixel_buffer buf;
            ((dng_simple_image *) image.Get ())->GetPixelBuffer (buf);
            for (uint32 r = 0; r < thumbH; r++)
                memcpy ((uint8 *) buf.fData + (size_t) r * buf.fRowStep,
                        bytes.data () + (size_t) r * thumbW * thumbCh, (size_t) thumbW * thumbCh);
            AutoPtr<dng_preview> preview (new dng_image_preview);
            fillInfo (preview->fInfo, appName, appVer, settingsName, settingsDigest,
                      previewDateTime.empty () ? dateTime : previewDateTime);
            preview->SetImage (host, image);
            previews->Append (preview);
            }

        if (!previewPath.empty ())
            {
            std::vector<uint8_t> bytes;
            if (!ReadFileBytes (previewPath.c_str (), bytes))
                { fprintf (stderr, "cannot read preview\n"); return 9; }
            AutoPtr<RawJpegPreview> preview (new RawJpegPreview);
            AutoPtr<dng_image> shape (new dng_simple_image (dng_rect (0, 0, (int32) prevH, (int32) prevW),
                                                           3, ttByte, host.Allocator ()));
            fillInfo (preview->fInfo, appName, appVer, settingsName, settingsDigest,
                      previewDateTime.empty () ? dateTime : previewDateTime);
            preview->SetImage (host, shape);
            AutoPtr<dng_memory_block> block (host.Allocator ().Allocate ((uint32) bytes.size ()));
            memcpy (block->Buffer (), bytes.data (), bytes.size ());
            AutoPtr<dng_memory_block> keep (block.Release ());
            preview->SetCompressedData (keep);
            dng_ifd &ifd = preview->IFD ();
            ifd.fCompression = ccJPEG;
            ifd.fPhotometricInterpretation = piYCbCr;
            ifd.fYCbCrSubSampleH = prevSubH;
            ifd.fYCbCrSubSampleV = prevSubV;
            ifd.fYCbCrPositioning = 0;
            ifd.fYCbCrCoefficientR = 0.299;
            ifd.fYCbCrCoefficientG = 0.587;
            ifd.fYCbCrCoefficientB = 0.114;
            ifd.fReferenceBlackWhite [0] = 0.0;
            ifd.fReferenceBlackWhite [1] = 255.0;
            ifd.fReferenceBlackWhite [2] = 128.0;
            ifd.fReferenceBlackWhite [3] = 255.0;
            ifd.fReferenceBlackWhite [4] = 128.0;
            ifd.fReferenceBlackWhite [5] = 255.0;
            previews->Append (reinterpret_cast<AutoPtr<dng_preview> &> (preview));
            }

        dng_file_stream out (outPath.c_str (), true);
        dng_image_writer writer;
        writer.WriteDNG (host, out, *negative,
                         previews->Count () ? previews.Get () : NULL,
                         dngVersion_SaveDefault, false, true);

        printf ("wrote %s\n", outPath.c_str ());
        return 0;
        }
    catch (const dng_exception &e)
        {
        fprintf (stderr, "dng exception %d\n", (int) e.ErrorCode ());
        return 7;
        }
    catch (...)
        {
        fprintf (stderr, "unknown exception\n");
        return 8;
        }
}
