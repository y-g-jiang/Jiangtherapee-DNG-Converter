

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <string>
#include <vector>

#include "dng_stream.h"
#include "dng_file_stream.h"
#include "dng_memory.h"
#include "dng_exceptions.h"
#include "dng_lossless_jpeg.h"
#include "dng_simd_type.h"

class VectorSpooler : public dng_spooler
{
public:
    std::vector<unsigned char> fData;
    void Spool (const void *data, uint32 count) override
    {
        const unsigned char *p = (const unsigned char *) data;
        fData.insert (fData.end (), p, p + count);
    }
};

struct SofInfo
{
    int precision = 0, width = 0, height = 0, ncomp = 0;
    std::vector<int> compId, hv, tq;
};

static bool parseSof (const std::vector<unsigned char> &b, SofInfo &so)
{
    size_t i = 0;
    while (i + 3 < b.size ())
        {
        if (b [i] != 0xFF) { i++; continue; }
        int m = b [i + 1];
        if (m == 0xD8 || m == 0xD9 || (m >= 0xD0 && m <= 0xD7)) { i += 2; continue; }
        int L = (b [i + 2] << 8) | b [i + 3];
        if (m == 0xC3 || m == 0xC1 || m == 0xC0)
            {
            size_t p = i + 4;
            so.precision = b [p];
            so.height = (b [p + 1] << 8) | b [p + 2];
            so.width  = (b [p + 3] << 8) | b [p + 4];
            so.ncomp  = b [p + 5];
            for (int c = 0; c < so.ncomp; c++)
                {
                so.compId.push_back (b [p + 6 + c * 3]);
                so.hv.push_back (b [p + 7 + c * 3]);
                so.tq.push_back (b [p + 8 + c * 3]);
                }
            return true;
            }
        if (m == 0xDA) return false;
        i += 2 + L;
        }
    return false;
}

static bool readFile (const char *path, std::vector<unsigned char> &out)
{
    FILE *f = fopen (path, "rb");
    if (!f) return false;
    fseek (f, 0, SEEK_END);
    long n = ftell (f);
    fseek (f, 0, SEEK_SET);
    out.resize ((size_t) n);
    bool ok = fread (out.data (), 1, out.size (), f) == out.size ();
    fclose (f);
    return ok;
}

int main (int argc, char **argv)
{
    if (argc < 3)
        {
        fprintf (stderr, "usage: %s info|dec|enc|rt ...\n", argv [0]);
        return 2;
        }
    std::string mode = argv [1];
    std::vector<unsigned char> in;
    if (!readFile (argv [2], in)) { fprintf (stderr, "cannot read %s\n", argv [2]); return 3; }

    if (mode == "batch")
        {

        if (argc < 5) { fprintf (stderr, "batch needs in list out\n"); return 2; }
        std::vector<unsigned char> whole;
        if (!readFile (argv [2], whole)) return 3;
        FILE *lf = fopen (argv [3], "rb");
        if (!lf) return 3;
        FILE *of = fopen (argv [4], "wb");
        if (!of) return 3;
        char line [256];
        long tiles = 0;
        while (fgets (line, sizeof (line), lf))
            {
            unsigned long long off = 0; unsigned long cnt = 0;
            if (sscanf (line, "%llu %lu", &off, &cnt) != 2) continue;
            if (off + cnt > whole.size ()) { fprintf (stderr, "range out of file\n"); break; }
            dng_memory_data inData (cnt);
            memcpy (inData.Buffer (), whole.data () + off, cnt);
            VectorSpooler spooler;
            try
                {
                dng_stream stream (inData.Buffer (), cnt);
                DecodeLosslessJPEG<Scalar> (stream, spooler, 0, 0x7FFFFFFFu, false,
                                            (uint64) cnt);
                }
            catch (const dng_exception &e)
                {
                fprintf (stderr, "tile %ld decode exception %d\n", tiles, (int) e.ErrorCode ());
                fclose (lf); fclose (of);
                return 5;
                }
            fwrite (spooler.fData.data (), 1, spooler.fData.size (), of);
            tiles++;
            }
        fclose (lf); fclose (of);
        printf ("tiles=%ld\n", tiles);
        return 0;
        }

    if (mode == "info")
        {
        SofInfo so;
        if (!parseSof (in, so)) { printf ("no SOF found\n"); return 4; }
        printf ("precision=%d width=%d height=%d ncomp=%d", so.precision, so.width, so.height, so.ncomp);
        for (int c = 0; c < so.ncomp; c++)
            printf (" [id=%d hv=%02X tq=%d]", so.compId [c], so.hv [c], so.tq [c]);
        printf ("\n");
        return 0;
        }

    if (mode == "dec" || mode == "rt")
        {
        SofInfo so;
        if (!parseSof (in, so)) { fprintf (stderr, "no SOF\n"); return 4; }
        uint32 channels = (uint32) so.ncomp;
        uint64 samples = (uint64) so.width * so.height * channels;
        uint32 bytes = (uint32) (samples * 2);

        dng_memory_data inData (uint32 (in.size ()));
        memcpy (inData.Buffer (), in.data (), in.size ());

        VectorSpooler spooler;
        try
            {
            dng_stream stream (inData.Buffer (), (uint32) in.size ());
            DecodeLosslessJPEG<Scalar> (stream, spooler, bytes, bytes, false, (uint64) in.size ());
            }
        catch (const dng_exception &e)
            {
            fprintf (stderr, "decode exception %d\n", (int) e.ErrorCode ());
            return 5;
            }
        if (spooler.fData.size () != bytes)
            {
            fprintf (stderr, "decoded %zu, expected %u\n", spooler.fData.size (), bytes);
            return 5;
            }
        const uint16 *px = (const uint16 *) spooler.fData.data ();
        double sum = 0; uint32 mn = 0xFFFF, mx = 0;
        size_t zeros = 0;
        for (uint64 k = 0; k < samples; k++)
            {
            uint32 v = px [k];
            sum += v; if (v < mn) mn = v; if (v > mx) mx = v;
            if (v == 0) zeros++;
            }
        printf ("decoded w=%d h=%d ch=%d samples=%llu min=%u max=%u mean=%.3f zeros=%zu\n",
                so.width, so.height, (int) channels, (unsigned long long) samples,
                mn, mx, sum / (double) samples, zeros);
        if (argc >= 4 && std::string (argv [3]) != "-")
            {
            FILE *f = fopen (argv [3], "wb");
            if (!f) return 6;
            fwrite (px, 2, (size_t) samples, f);
            fclose (f);
            }

        if (mode == "dec")
            return 0;

        if (argc < 4) { fprintf (stderr, "rt needs an output path\n"); return 2; }
        try
            {
            dng_file_stream out (argv [3], true);
            EncodeLosslessJPEG<Scalar> (px, (uint32) so.height, (uint32) so.width,
                                        channels, (uint32) so.precision,
                                        (int32) (so.width * channels), (int32) channels, out);
            out.Flush ();
            }
        catch (const dng_exception &e)
            {
            fprintf (stderr, "encode exception %d\n", (int) e.ErrorCode ());
            return 6;
            }
        std::vector<unsigned char> re;
        readFile (argv [3], re);
        bool same = re.size () == in.size ();
        size_t fd = 0;
        if (same)
            for (size_t k = 0; k < in.size (); k++)
                if (in [k] != re [k]) { same = false; fd = k; break; }
        printf ("rt in=%zu out=%zu %s", in.size (), re.size (), same ? "IDENTICAL" : "DIFFERENT");
        if (!same && fd) printf (" firstdiff=%zu in=%02x out=%02x", fd, in [fd], re [fd]);
        printf ("\n");
        return 0;
        }

    if (mode == "enc")
        {
        if (argc < 8) { fprintf (stderr, "enc needs raw out w h ch bits\n"); return 2; }
        uint32 w = (uint32) atoi (argv [4]), h = (uint32) atoi (argv [5]);
        uint32 ch = (uint32) atoi (argv [6]), bits = (uint32) atoi (argv [7]);
        if (in.size () != (size_t) w * h * ch * 2) { fprintf (stderr, "size mismatch\n"); return 7; }
        try
            {
            dng_file_stream out (argv [3], true);
            EncodeLosslessJPEG<Scalar> ((const uint16 *) in.data (), h, w, ch, bits,
                                        (int32) (w * ch), 1, out);
            out.Flush ();
            }
        catch (const dng_exception &e)
            {
            fprintf (stderr, "encode exception %d\n", (int) e.ErrorCode ());
            return 6;
            }
        printf ("encoded -> %s\n", argv [3]);
        return 0;
        }

    fprintf (stderr, "unknown mode\n");
    return 2;
}

