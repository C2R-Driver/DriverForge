// SPDX-License-Identifier: MPL-2.0
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/fs.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

static int result(const char *c, const char *s, const char *d) {
    printf("STORAGE_SEMANTIC case=%s status=%s detail=%s\n", c, s, d);
    return strcmp(s, "PASS") != 0;
}
static int rw(int fd, unsigned seed, size_t size) {
    unsigned char *a=NULL,*b=NULL;
    if(posix_memalign((void**)&a,4096,size) || posix_memalign((void**)&b,4096,size)) exit(2);
    for(size_t i=0;i<size;i++) a[i]=(i*29+seed)&255;
    int ok=pwrite(fd,a,size,1048576)==(ssize_t)size && fsync(fd)==0 && pread(fd,b,size,1048576)==(ssize_t)size && !memcmp(a,b,size);
    free(a);free(b);return ok;
}
int main(int argc,char **argv) {
    if(argc<2) return 2;
    const char *c=argv[1];
    if(!strcmp(c,"malformed")) {
        int fd=open("/dev/vdc",O_RDONLY|O_DIRECT);
        if(fd<0) return result(c,"FAIL","disk-missing");
        unsigned char *b=NULL;if(posix_memalign((void**)&b,4096,4096))return 2;
        memset(b,0xa5,4096);
        puts("BLOCK_MALFORMED_READY");fflush(stdout);
        if(getchar()==EOF)return 2;
        ssize_t n=pread(fd,b,4096,1048576);int error=errno;
        printf("BLOCK_MALFORMED_RESULT bytes=%zd errno=%d\n",n,error);fflush(stdout);
        free(b);close(fd);
        return result(c,n<0?"PASS":"FAIL","no-success-with-unwritten-status");
    }
    if(!strcmp(c,"capacity")) {
        if(argc!=3) return 2;
        int fd=open("/dev/vdc",O_RDONLY); uint64_t n=0;
        int ok=fd>=0 && ioctl(fd,BLKGETSIZE64,&n)==0;
        printf("CAPACITY observed=%llu expected=%s errno=%d\n",(unsigned long long)n,argv[2],errno);
        if(fd>=0)close(fd);
        return result(c,ok && n==strtoull(argv[2],NULL,10)?"PASS":"FAIL","live-size");
    }
    if(!strcmp(c,"readonly")) {
        int fd=open("/dev/vdc",O_RDWR|O_DIRECT);
        if(fd<0 && (errno==EROFS||errno==EACCES)) return result(c,"PASS","open-rejected");
        if(fd<0) return result(c,"FAIL","cannot-open");
        unsigned char *a=NULL,*b=NULL;
        if(posix_memalign((void**)&a,4096,4096)||posix_memalign((void**)&b,4096,4096))return 2;
        memset(a,0x5a,4096);memset(b,0xaa,4096);
        ssize_t w=pwrite(fd,a,4096,1048576);int e=errno;
        ssize_t r=pread(fd,b,4096,1048576);int zero=1;
        for(int i=0;i<4096;i++)if(b[i])zero=0;
        printf("READONLY write=%zd errno=%d read=%zd unchanged=%d\n",w,e,r,zero);
        free(a);free(b);close(fd);
        return result(c,w<0&&r==4096&&zero?"PASS":"FAIL","no-false-write-success");
    }
    if(!strcmp(c,"timeout")) {
        int fd=open("/dev/nvme0n1",O_RDONLY|O_DIRECT);
        if(fd<0) return result(c,"FAIL","namespace1-missing");
        unsigned char *b=NULL;if(posix_memalign((void**)&b,4096,4096))return 2;
        puts("NVME_TIMEOUT_READ_READY");fflush(stdout);
        if(getchar()==EOF)return 2;
        ssize_t n=pread(fd,b,4096,1048576);int error=errno;
        if(n==4096){n=pread(fd,b,4096,1052672);error=errno;}
        printf("NVME_TIMEOUT_RESULT bytes=%zd errno=%d\n",n,error);fflush(stdout);
        free(b);close(fd);
        return result(c,n<0?"PASS":"FAIL","bounded-error-on-uncompleted-read");
    }
    if(!strcmp(c,"largeio")) {
        int fd=open("/dev/nvme0n1",O_RDWR|O_DIRECT);
        if(fd<0) return result(c,"FAIL","namespace1-missing");
        int ok=rw(fd,53,131072)&&rw(fd,171,65536);close(fd);
        return result(c,ok?"PASS":"FAIL","multi-page-prp-data");
    }
    if(!strcmp(c,"namespaces")) {
        int f[2]={open("/dev/nvme0n1",O_RDWR|O_DIRECT),open("/dev/nvme0n2",O_RDWR|O_DIRECT)};
        if(f[0]<0) return result(c,"FAIL","namespace1-missing");
        if(f[1]<0) {close(f[0]);return result(c,"UNSUPPORTED","namespace2-missing");}
        int ok=rw(f[0],17,65536)&&rw(f[1],193,65536);
        unsigned char *b=NULL;if(posix_memalign((void**)&b,4096,65536))return 2;
        for(int n=0;n<2;n++){
            if(pread(f[n],b,65536,1048576)!=65536){ok=0;continue;}
            for(size_t i=0;i<65536;i++) if(b[i]!=(unsigned char)(i*29+(n?193:17)))ok=0;
            close(f[n]);
        }
        free(b);return result(c,ok?"PASS":"FAIL","namespace-data-isolation");
    }
    return 2;
}
