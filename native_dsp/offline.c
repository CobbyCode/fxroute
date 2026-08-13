#include "dsp.h"
#include <stdio.h>
#include <stdlib.h>

int main(int argc,char**argv){
    char error[256]; if(argc!=4&&argc!=5){fprintf(stderr,"usage: %s CONFIG INPUT.f32 OUTPUT.f32 [QUANTUM]\n",argv[0]);return 2;}
    fxdsp*d=fxdsp_load(argv[1],error,sizeof error);if(!d){fprintf(stderr,"%s\n",error);return 1;}
    FILE*in=fopen(argv[2],"rb"),*out=fopen(argv[3],"wb");if(!in||!out){perror("audio file");return 1;}
    size_t block=argc==5?strtoul(argv[4],NULL,10):1024;if(!block){fprintf(stderr,"invalid quantum\n");return 2;}float *ib[32]={0},*ob[32]={0};const float*ic[32];for(unsigned c=0;c<fxdsp_inputs(d);c++){ib[c]=calloc(block,sizeof(float));ic[c]=ib[c];}for(unsigned c=0;c<fxdsp_outputs(d);c++)ob[c]=calloc(block,sizeof(float));
    float *packed=malloc(block*fxdsp_inputs(d)*sizeof(float));size_t values;
    while((values=fread(packed,sizeof(float),block*fxdsp_inputs(d),in))){size_t frames=values/fxdsp_inputs(d);for(size_t n=0;n<frames;n++)for(unsigned c=0;c<fxdsp_inputs(d);c++)ib[c][n]=packed[n*fxdsp_inputs(d)+c];fxdsp_process(d,ic,ob,frames);for(size_t n=0;n<frames;n++)for(unsigned c=0;c<fxdsp_outputs(d);c++)fwrite(&ob[c][n],sizeof(float),1,out);}
    for(unsigned c=0;c<fxdsp_outputs(d);c++){float peak,rms;fxdsp_meter(d,c,&peak,&rms);printf("meter %u peak %.9g rms %.9g\n",c,peak,rms);free(ob[c]);}for(unsigned c=0;c<fxdsp_inputs(d);c++)free(ib[c]);free(packed);fclose(in);fclose(out);fxdsp_free(d);return 0;
}
