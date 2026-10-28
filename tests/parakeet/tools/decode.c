/* D16 C-API check: decode raw float32 16 kHz files, print tokens/timestamps/durations as JSON lines. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "sherpa-onnx/c-api/c-api.h"
static void js(const char *p) { putchar('"'); for (; *p; p++) { if (*p == '"' || *p == '\\') putchar('\\'); putchar(*p); } putchar('"'); }
int main(int argc, char **argv) {
  if (argc < 4) return 2;
  const char *dir = argv[1]; int threads = atoi(argv[2]);
  char enc[4096], dec[4096], joi[4096], tok[4096];
  snprintf(enc, sizeof enc, "%s/encoder.int8.onnx", dir); snprintf(dec, sizeof dec, "%s/decoder.int8.onnx", dir);
  snprintf(joi, sizeof joi, "%s/joiner.int8.onnx", dir); snprintf(tok, sizeof tok, "%s/tokens.txt", dir);
  SherpaOnnxOfflineRecognizerConfig c; memset(&c, 0, sizeof c);
  c.feat_config.sample_rate = 16000; c.feat_config.feature_dim = 128;
  c.model_config.transducer.encoder = enc; c.model_config.transducer.decoder = dec; c.model_config.transducer.joiner = joi;
  c.model_config.tokens = tok; c.model_config.num_threads = threads; c.model_config.provider = "cpu";
  c.model_config.model_type = "nemo_transducer"; c.decoding_method = "greedy_search";
  const SherpaOnnxOfflineRecognizer *r = SherpaOnnxCreateOfflineRecognizer(&c);
  if (!r) { fprintf(stderr, "create failed\n"); return 1; }
  for (int i = 3; i < argc; i++) {
    FILE *f = fopen(argv[i], "rb"); fseek(f, 0, SEEK_END); long n = ftell(f) / 4; fseek(f, 0, SEEK_SET);
    float *s = malloc(n * 4); if (fread(s, 4, n, f) != (size_t)n) return 3; fclose(f);
    const SherpaOnnxOfflineStream *st = SherpaOnnxCreateOfflineStream(r);
    SherpaOnnxAcceptWaveformOffline(st, 16000, s, n);
    SherpaOnnxDecodeOfflineStream(r, st);
    const SherpaOnnxOfflineRecognizerResult *res = SherpaOnnxGetOfflineStreamResult(st);
    printf("{\"text\":"); js(res->text); printf(",\"tokens\":[");
    for (int k = 0; k < res->count; k++) { if (k) putchar(','); js(res->tokens_arr[k]); }
    printf("],\"timestamps\":[");
    for (int k = 0; k < res->count; k++) printf("%s%.9g", k ? "," : "", (double)res->timestamps[k]);
    printf("],\"durations\":[");
    for (int k = 0; k < res->count; k++) printf("%s%.9g", k ? "," : "", (double)res->durations[k]);
    printf("]}\n");
    SherpaOnnxDestroyOfflineRecognizerResult(res); SherpaOnnxDestroyOfflineStream(st); free(s);
  }
  SherpaOnnxDestroyOfflineRecognizer(r);
  return 0;
}
