// SPDX-License-Identifier: MPL-2.0
#include <arpa/inet.h>
#include <errno.h>
#include <stdio.h>
#include <sys/socket.h>
#include <unistd.h>
int main(void) {
    int fd=socket(AF_INET,SOCK_DGRAM,0);
    struct sockaddr_in addr={.sin_family=AF_INET,.sin_port=htons(5207),.sin_addr={.s_addr=htonl(0x0a00020f)}};
    if(fd<0 || bind(fd,(struct sockaddr*)&addr,sizeof(addr))) {perror("udp-bind");return 2;}
    puts("UDP_SEMANTIC_READY");fflush(stdout);
    for(;;){
        unsigned char data[2048];struct sockaddr_in peer;socklen_t n=sizeof(peer);
        ssize_t r=recvfrom(fd,data,sizeof(data),0,(struct sockaddr*)&peer,&n);
        if(r<0){if(errno==EINTR)continue;perror("recvfrom");return 3;}
        if(sendto(fd,data,r,0,(struct sockaddr*)&peer,n)!=r){perror("sendto");return 4;}
    }
}
