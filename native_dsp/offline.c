#include "dsp.h"
#include <stdio.h>
#include <stdlib.h>

int main(int argc,char**argv){
    char error[256]; if(argc!=4&&argc!=5){fprintf(stderr,"usage: %s CONFIG INPUT.f32 OUTPUT.f32 [QUANTUM]\n",argv[0]);return 2;}
    fxdsp*d=fxdsp_load(argv[1],error,sizeof error);if(!d){fprintf(stderr,"%s\n",error);return 1;}
    unsigned inputs=fxdsp_inputs(d),outputs=fxdsp_outputs(d);
    if(inputs<1||inputs>FXDSP_MAX_CHANNELS||outputs<1||outputs>FXDSP_MAX_CHANNELS){fprintf(stderr,"config channels must be 1..%u (inputs=%u outputs=%u)\n",FXDSP_MAX_CHANNELS,inputs,outputs);fxdsp_free(d);return 1;}
    FILE*in=fopen(argv[2],"rb"),*out=fopen(argv[3],"wb");if(!in||!out){perror("audio file");return 1;}
    size_t block=argc==5?strtoul(argv[4],NULL,10):1024;if(!block){fprintf(stderr,"invalid quantum\n");return 2;}float *ib[FXDSP_MAX_CHANNELS]={0},*ob[FXDSP_MAX_CHANNELS]={0};const float*ic[FXDSP_MAX_CHANNELS];for(unsigned c=0;c<inputs;c++){ib[c]=calloc(block,sizeof(float));ic[c]=ib[c];}for(unsigned c=0;c<outputs;c++)ob[c]=calloc(block,sizeof(float));
    float *packed=malloc(block*inputs*sizeof(float));size_t values;
    while((values=fread(packed,sizeof(float),block*inputs,in))){size_t frames=values/inputs;for(size_t n=0;n<frames;n++)for(unsigned c=0;c<inputs;c++)ib[c][n]=packed[n*inputs+c];fxdsp_process(d,ic,ob,frames);for(size_t n=0;n<frames;n++)for(unsigned c=0;c<outputs;c++)fwrite(&ob[c][n],sizeof(float),1,out);}
    for(unsigned c=0;c<outputs;c++){float peak,rms;fxdsp_meter(d,c,&peak,&rms);printf("meter %u peak %.9g rms %.9g\n",c,peak,rms);free(ob[c]);}for(unsigned c=0;c<inputs;c++)free(ib[c]);free(packed);fclose(in);fclose(out);fxdsp_free(d);return 0;
}
