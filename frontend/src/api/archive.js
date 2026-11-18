import{api,post}from'./client.js';
export const overview=id=>api('/trials/'+id+'/archive');
export const query=(id,payload)=>post('/trials/'+id+'/archive/query',payload);
