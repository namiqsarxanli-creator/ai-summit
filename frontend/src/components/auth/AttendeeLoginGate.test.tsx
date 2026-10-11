import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { AttendeeLoginGate } from "./AttendeeLoginGate";
const router=vi.hoisted(()=>({replace:vi.fn()}));
vi.mock("next/navigation",()=>({useRouter:()=>router}));
afterEach(()=>{vi.unstubAllGlobals();router.replace.mockReset();});
it("returns a valid session home without displaying or requesting OTP",async()=>{
 const fetch=vi.fn(async()=>new Response(JSON.stringify({first_name:"A",last_name:"B",email:"a@example.com",profession:"AI"}),{status:200}));vi.stubGlobal("fetch",fetch);
 render(<AttendeeLoginGate><p>OTP form</p></AttendeeLoginGate>);
 await waitFor(()=>expect(router.replace).toHaveBeenCalledWith("/"));
 expect(screen.queryByText("OTP form")).not.toBeInTheDocument();expect(fetch.mock.calls).toHaveLength(1);
});
it("shows the login form when the session is expired",async()=>{
 vi.stubGlobal("fetch",vi.fn(async()=>new Response(JSON.stringify({error:{code:"UNAUTHENTICATED",message:"",details:{}}}),{status:401})));
 render(<AttendeeLoginGate><p>OTP form</p></AttendeeLoginGate>);expect(await screen.findByText("OTP form")).toBeInTheDocument();expect(router.replace).not.toHaveBeenCalled();
});
