using System;
using System.Text;

namespace WindowsMcp.Text
{
    // UTF-8 without a BOM that writes LF where the text has CRLF.
    // A CR that is not followed by LF is kept.
    public sealed class LfUtf8Encoding : Encoding
    {
        private static readonly UTF8Encoding Utf8 = new UTF8Encoding(false);

        public LfUtf8Encoding() : base(65001) { }

        // Drops each CR that is followed by LF. A CR at the end of the input
        // is held in pendingCr until the next input shows what follows it,
        // and written out as a CR when flush is set.
        internal static char[] Strip(char[] chars, int index, int count, ref bool pendingCr, bool flush)
        {
            var sb = new StringBuilder(count + 1);
            bool pending = pendingCr;
            for (int i = index; i < index + count; i++)
            {
                char c = chars[i];
                if (pending)
                {
                    if (c != '\n') sb.Append('\r');
                    pending = false;
                }
                if (c == '\r') { pending = true; continue; }
                sb.Append(c);
            }
            if (pending && flush) { sb.Append('\r'); pending = false; }
            pendingCr = pending;
            return sb.ToString().ToCharArray();
        }

        public override int GetByteCount(char[] chars, int index, int count)
        {
            bool pending = false;
            char[] f = Strip(chars, index, count, ref pending, true);
            return Utf8.GetByteCount(f, 0, f.Length);
        }

        public override int GetBytes(char[] chars, int charIndex, int charCount, byte[] bytes, int byteIndex)
        {
            bool pending = false;
            char[] f = Strip(chars, charIndex, charCount, ref pending, true);
            return Utf8.GetBytes(f, 0, f.Length, bytes, byteIndex);
        }

        public override int GetCharCount(byte[] bytes, int index, int count)
        {
            return Utf8.GetCharCount(bytes, index, count);
        }

        public override int GetChars(byte[] bytes, int byteIndex, int byteCount, char[] chars, int charIndex)
        {
            return Utf8.GetChars(bytes, byteIndex, byteCount, chars, charIndex);
        }

        // One more char than asked: a CR held back from the previous input.
        public override int GetMaxByteCount(int charCount) { return Utf8.GetMaxByteCount(charCount + 1); }
        public override int GetMaxCharCount(int byteCount) { return Utf8.GetMaxCharCount(byteCount); }
        public override Encoder GetEncoder() { return new LfEncoder(); }
        public override Decoder GetDecoder() { return Utf8.GetDecoder(); }
        public override byte[] GetPreamble() { return new byte[0]; }
        public override ReadOnlySpan<byte> Preamble { get { return ReadOnlySpan<byte>.Empty; } }
    }

    // Stateful counterpart used by StreamWriter: a CRLF split across two
    // writes still becomes LF.
    internal sealed class LfEncoder : Encoder
    {
        private readonly Encoder _inner = new UTF8Encoding(false).GetEncoder();
        private bool _pendingCr;

        public override int GetByteCount(char[] chars, int index, int count, bool flush)
        {
            bool pending = _pendingCr;
            char[] f = LfUtf8Encoding.Strip(chars, index, count, ref pending, flush);
            return _inner.GetByteCount(f, 0, f.Length, flush);
        }

        public override int GetBytes(char[] chars, int charIndex, int charCount, byte[] bytes, int byteIndex, bool flush)
        {
            char[] f = LfUtf8Encoding.Strip(chars, charIndex, charCount, ref _pendingCr, flush);
            return _inner.GetBytes(f, 0, f.Length, bytes, byteIndex, flush);
        }

        public override void Reset()
        {
            _pendingCr = false;
            _inner.Reset();
        }
    }
}
