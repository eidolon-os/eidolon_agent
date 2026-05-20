import axios from "axios";

export function client() {
  return axios.create({ baseURL: "/api" });
}
