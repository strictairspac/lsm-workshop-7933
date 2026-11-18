import{overview,query}from'../api/archive.js';
export const archiveStore={
  async meta(id){return overview(id)},
  async rangeQuery(id,payload){return query(id,payload)}
};
